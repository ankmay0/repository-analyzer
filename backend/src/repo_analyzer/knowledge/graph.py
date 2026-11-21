"""Build the workspace knowledge graph from file outlines.

Nodes are repositories, files and module-level symbols. Edges:

* ``depends_on``  repository -> repository   (declared in a package manifest)
* ``defines``     file -> symbol
* ``imports``     file -> file               (an import that resolves to an indexed file)
* ``uses``        file -> symbol             (an imported name that resolves to a definition)

``imports`` and ``uses`` cross repository boundaries. That is the point of the
graph: it records not just that flask depends on markupsafe, but which flask
files import which markupsafe definitions.

Resolution is static and conservative. An import is linked only when it resolves
to exactly one indexed module; anything else (third-party packages, the standard
library, dynamic imports) produces no edge. Calls are not tracked: without type
information they cannot be resolved reliably in Python.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from repo_analyzer.ingestion.dependency_graph import DependencyGraph
from repo_analyzer.knowledge import SCHEMA_VERSION
from repo_analyzer.knowledge.outline import FileOutline, Import, Symbol

_MAX_REEXPORT_DEPTH = 6


def repo_id(repo: str) -> str:
    return f"repo:{repo}"


def file_id(repo: str, path: str) -> str:
    return f"file:{repo}/{path}"


def symbol_id(repo: str, path: str, qualname: str) -> str:
    return f"symbol:{repo}/{path}#{qualname}"


class _Resolver:
    """Resolves import statements to indexed files and symbols."""

    def __init__(
        self, outlines: Mapping[str, Sequence[FileOutline]], declared: set[tuple[str, str]]
    ) -> None:
        self._declared = declared
        self._modules: dict[str, dict[str, FileOutline]] = {}
        self._packages: dict[str, set[str]] = {}
        for repo, files in outlines.items():
            modules: dict[str, FileOutline] = {}
            for outline in files:
                if outline.module is not None:
                    # First path wins if two files claim one module name (sorted input).
                    modules.setdefault(outline.module, outline)
            self._modules[repo] = modules
            self._packages[repo] = {
                name for name, outline in modules.items() if outline.is_package and "." not in name
            }

    def module(self, from_repo: str, dotted: str) -> FileOutline | None:
        """The indexed file that ``import dotted`` refers to from ``from_repo``."""
        if not dotted:
            return None
        own = self._modules[from_repo].get(dotted)
        if own is not None:
            return own
        candidates = [
            outline
            for repo, modules in self._modules.items()
            if repo != from_repo and (outline := modules.get(dotted)) is not None
        ]
        if len(candidates) != 1:
            return None
        target = candidates[0]
        # Guard against coincidental names such as a stray ``utils.py`` in another
        # repository: link across repositories only into a real top-level package,
        # or where a manifest declares the dependency.
        top_level = dotted.split(".")[0]
        if (from_repo, target.repo) in self._declared or top_level in self._packages[target.repo]:
            return target
        return None

    def absolute_module(self, importer: FileOutline, statement: Import) -> str | None:
        """The absolute dotted module an import statement names."""
        if statement.level == 0:
            return statement.module
        if importer.module is None:
            return None
        package = importer.module.split(".")
        if not importer.is_package:
            package = package[:-1]
        ascend = statement.level - 1
        if ascend > len(package):
            return None
        base = package[: len(package) - ascend]
        return ".".join(base + ([statement.module] if statement.module else []))

    def symbol(
        self, module: FileOutline, name: str, _depth: int = 0
    ) -> tuple[FileOutline, Symbol] | None:
        """Find where ``name``, as exported by ``module``, is actually defined.

        Follows re-exports, so ``from flask import Flask`` resolves through
        ``flask/__init__.py`` to the class in ``flask/app.py``.
        """
        defined = module.top_level(name)
        if defined is not None:
            return module, defined
        if _depth >= _MAX_REEXPORT_DEPTH:
            return None
        for statement in module.imports:
            if statement.name is None:
                continue
            is_star = statement.name == "*"
            if not is_star and statement.bound_name != name:
                continue
            dotted = self.absolute_module(module, statement)
            source = self.module(module.repo, dotted) if dotted else None
            if source is None or source is module:
                continue
            found = self.symbol(source, name if is_star else statement.name, _depth + 1)
            if found is not None:
                return found
        return None

    def resolve(
        self, importer: FileOutline, statement: Import
    ) -> tuple[FileOutline | None, tuple[FileOutline, Symbol] | None]:
        """Return ``(imported file, imported symbol)`` for one import; either may be None."""
        dotted = self.absolute_module(importer, statement)
        if not dotted:
            return None, None
        if statement.name is None:
            # ``import a.b.c`` binds ``a`` but loads a.b.c; link the deepest indexed module.
            parts = dotted.split(".")
            for end in range(len(parts), 0, -1):
                target = self.module(importer.repo, ".".join(parts[:end]))
                if target is not None:
                    return target, None
            return None, None
        if statement.name != "*":
            submodule = self.module(importer.repo, f"{dotted}.{statement.name}")
            if submodule is not None:  # ``from package import module``
                return submodule, None
        target = self.module(importer.repo, dotted)
        if target is None or statement.name == "*":
            return target, None
        return target, self.symbol(target, statement.name)


def build_graph(
    outlines: Mapping[str, Sequence[FileOutline]],
    commits: Mapping[str, str | None],
    dependencies: DependencyGraph,
) -> dict[str, Any]:
    """Assemble the graph document. ``outlines`` maps repository name to its files."""
    declared = {(e.source_repo, e.target_repo) for e in dependencies.edges}
    resolver = _Resolver(outlines, declared)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = [
        {
            "kind": "depends_on",
            "source": repo_id(e.source_repo),
            "target": repo_id(e.target_repo),
            "ecosystem": e.package.ecosystem.value,
            "package": e.package.name,
            "scope": e.scope.value,
            "manifest_path": e.manifest_path,
        }
        for e in dependencies.edges
    ]
    imports: dict[tuple[str, str], int] = {}
    uses: dict[tuple[str, str], tuple[int, str]] = {}

    for repo in sorted(outlines):
        nodes.append(
            {"id": repo_id(repo), "kind": "repo", "name": repo, "commit": commits.get(repo)}
        )
        for outline in outlines[repo]:
            source = file_id(repo, outline.path)
            nodes.append(
                {"id": source, "kind": "file", "repo": repo, "path": outline.path,
                 "language": outline.language, "module": outline.module}
            )  # fmt: skip
            for symbol in _graph_symbols(outline.symbols):
                target = symbol_id(repo, outline.path, symbol.qualname)
                nodes.append(
                    {"id": target, "kind": "symbol", "repo": repo, "path": outline.path,
                     "name": symbol.name, "symbol_kind": symbol.kind, "line": symbol.line}
                )  # fmt: skip
                edges.append({"kind": "defines", "source": source, "target": target})

            for statement in outline.imports:
                imported_file, imported_symbol = resolver.resolve(outline, statement)
                if imported_file is not None and imported_file is not outline:
                    key = (source, file_id(imported_file.repo, imported_file.path))
                    imports[key] = min(imports.get(key, statement.line), statement.line)
                if imported_symbol is not None:
                    owner, symbol = imported_symbol
                    if owner is not outline:
                        key = (source, symbol_id(owner.repo, owner.path, symbol.qualname))
                        if key not in uses or statement.line < uses[key][0]:
                            uses[key] = (statement.line, symbol.name)

    edges += [
        {"kind": "imports", "source": s, "target": t, "line": line}
        for (s, t), line in sorted(imports.items())
    ]
    edges += [
        {"kind": "uses", "source": s, "target": t, "line": line, "name": name}
        for (s, t), (line, name) in sorted(uses.items())
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "node_kinds": ["repo", "file", "symbol"],
        "edge_kinds": ["depends_on", "defines", "imports", "uses"],
        "nodes": nodes,
        "edges": edges,
    }


def _graph_symbols(symbols: Iterable[Symbol]) -> Iterable[Symbol]:
    """Module-level symbols only; methods are in the map, not the graph."""
    return (s for s in symbols if s.kind != "method")
