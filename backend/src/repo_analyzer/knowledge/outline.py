"""Extract a file's structure: the symbols it defines and the modules it imports.

Only Python is parsed, using the standard library's ``ast`` module, which reads
source without executing it. Files in other languages get an outline with no
symbols, so they still appear in the pack and the map as files.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from repo_analyzer.domain import SourceFile

SymbolKind = Literal["class", "function", "method", "variable"]

_MAX_SIGNATURE_CHARS = 200
_MAX_DOC_CHARS = 140


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    qualname: str  # "Flask" or "Flask.route"
    kind: SymbolKind
    signature: str
    line: int
    end_line: int
    doc: str  # first line of the docstring, or ""


@dataclass(frozen=True, slots=True)
class Import:
    """One imported name. ``import a.b`` has ``name=None``; ``from a import b`` has ``name="b"``."""

    module: str  # dotted module as written, without leading dots
    name: str | None
    alias: str | None
    level: int  # number of leading dots in a relative import
    line: int

    @property
    def bound_name(self) -> str | None:
        """The name this import makes available in the importing module."""
        if self.name is None:
            return self.alias or self.module.split(".")[0]
        return self.alias or self.name


@dataclass(frozen=True, slots=True)
class FileOutline:
    repo: str
    path: str
    language: str
    lines: int
    module: str | None = None  # dotted Python module name, if this is a Python module
    is_package: bool = False  # True for a package's __init__.py
    symbols: tuple[Symbol, ...] = ()
    imports: tuple[Import, ...] = ()
    _top_level: dict[str, Symbol] = field(default_factory=dict, repr=False, compare=False)

    def top_level(self, name: str) -> Symbol | None:
        """The module-level symbol called ``name``, if this file defines one."""
        if not self._top_level:
            self._top_level.update(
                {s.name: s for s in reversed(self.symbols) if s.kind != "method"}
            )
        return self._top_level.get(name)


def module_name(path: str, package_dirs: frozenset[str]) -> tuple[str, bool] | None:
    """Return ``(dotted module name, is_package)`` for a Python file path.

    The name is built from the enclosing chain of package directories (those
    containing an ``__init__.py``), so ``src/flask/app.py`` is ``flask.app``
    when ``src/flask`` is a package and ``src`` is not.
    """
    if not path.endswith(".py"):
        return None
    *directories, filename = path.split("/")
    stem = filename[: -len(".py")]
    start = len(directories)
    while start > 0 and "/".join(directories[:start]) in package_dirs:
        start -= 1
    is_package = stem == "__init__"
    parts = directories[start:] + ([] if is_package else [stem])
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts), is_package


def outline_file(file: SourceFile, package_dirs: frozenset[str]) -> FileOutline:
    lines = file.text.count("\n") + (0 if file.text.endswith("\n") else 1)
    if file.language != "python":
        return FileOutline(repo=file.repo, path=file.path, language=file.language, lines=lines)

    named = module_name(file.path, package_dirs)
    module, is_package = named if named else (None, False)
    try:
        tree = ast.parse(file.text)
    except (SyntaxError, ValueError, RecursionError):
        # Not valid for this Python version (templates, Python 2 code, pathological
        # nesting). Keep the file; it just has no structure to report.
        return FileOutline(file.repo, file.path, file.language, lines, module, is_package)

    return FileOutline(
        repo=file.repo,
        path=file.path,
        language=file.language,
        lines=lines,
        module=module,
        is_package=is_package,
        symbols=tuple(_symbols(tree)),
        imports=tuple(_imports(tree)),
    )


def _module_statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Module-level statements, looking inside ``if``/``try``/``with`` blocks.

    Compatibility shims and ``if TYPE_CHECKING:`` blocks define module-level
    names inside such blocks.
    """
    for statement in body:
        if isinstance(statement, ast.If):
            yield from _module_statements(statement.body)
            yield from _module_statements(statement.orelse)
        elif isinstance(statement, ast.Try):
            yield from _module_statements(statement.body)
            for handler in statement.handlers:
                yield from _module_statements(handler.body)
            yield from _module_statements(statement.orelse)
            yield from _module_statements(statement.finalbody)
        elif isinstance(statement, ast.With):
            yield from _module_statements(statement.body)
        else:
            yield statement


def _symbols(tree: ast.Module) -> Iterator[Symbol]:
    for statement in _module_statements(tree.body):
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            yield _function_symbol(statement, parent=None)
        elif isinstance(statement, ast.ClassDef):
            bases = ", ".join(ast.unparse(base) for base in statement.bases)
            signature = f"class {statement.name}({bases})" if bases else f"class {statement.name}"
            yield Symbol(
                name=statement.name,
                qualname=statement.name,
                kind="class",
                signature=_clip(signature, _MAX_SIGNATURE_CHARS),
                line=statement.lineno,
                end_line=statement.end_lineno or statement.lineno,
                doc=_first_doc_line(statement),
            )
            for member in statement.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    yield _function_symbol(member, parent=statement.name)
        elif isinstance(statement, ast.Assign | ast.AnnAssign):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield Symbol(
                        name=target.id,
                        qualname=target.id,
                        kind="variable",
                        signature=target.id,
                        line=statement.lineno,
                        end_line=statement.end_lineno or statement.lineno,
                        doc="",
                    )


def _function_symbol(node: ast.FunctionDef | ast.AsyncFunctionDef, parent: str | None) -> Symbol:
    keyword = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    signature = f"{keyword} {node.name}({ast.unparse(node.args)}){returns}"
    return Symbol(
        name=node.name,
        qualname=f"{parent}.{node.name}" if parent else node.name,
        kind="method" if parent else "function",
        signature=_clip(signature, _MAX_SIGNATURE_CHARS),
        line=node.lineno,
        end_line=node.end_lineno or node.lineno,
        doc=_first_doc_line(node),
    )


def _imports(tree: ast.Module) -> Iterator[Import]:
    # The whole tree, not just the top level: imports inside functions and
    # ``if TYPE_CHECKING:`` blocks are dependencies too.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield Import(alias.name, None, alias.asname, 0, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                yield Import(node.module or "", alias.name, alias.asname, node.level, node.lineno)


def _first_doc_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    doc = ast.get_docstring(node) or ""
    return _clip(doc.strip().split("\n", 1)[0].strip(), _MAX_DOC_CHARS)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
