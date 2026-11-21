"""Tools the agents use to inspect the indexed repositories.

There are two sets, matching the two kinds of agent:

* graph tools, for the hub: what is indexed and how the repositories depend on
  each other. They never return file contents.
* repository tools, for a repository expert: search and read the code of exactly
  one repository. The repository is fixed when the tools are built and is not a
  parameter, so an expert cannot be talked into reading another repository.

Each set comes in two variants, selected by ``Settings.knowledge_mode``. In
``json`` mode the tools read the static pack, map and graph files. In ``rag``
mode the expert searches a vector store instead and the hub has no symbol-level
graph.

Tools are built by factories that close over their dependencies, so there is no
module-level state and tests can build them against a temporary workspace.

Each tool returns ``(content, artifact)``: ``content`` is the text the model
reads; ``artifact`` is a list of structured source references that the API
streams to the UI and that never enters the model's context.

Expected failures (unknown repository, bad path) are returned to the model as
ordinary text so it can correct itself and retry.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import BaseTool, tool

from repo_analyzer.config import Settings
from repo_analyzer.domain import DependencyEdge
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.ingestion.scanner import language_for, scan_repository
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace

MAX_READ_LINES = 400
MAX_REFERENCE_HITS = 60
MAX_REFERENCE_LINE_CHARS = 240
MAX_SYMBOL_MATCHES = 40
MAX_USAGE_LINES = 120

Source = dict[str, Any]
ToolOutput = tuple[str, list[Source]]


def _source(repo: str, path: str, start_line: int, end_line: int) -> Source:
    return {"repo": repo, "path": path, "start_line": start_line, "end_line": end_line}


def _describe_edge(edge: DependencyEdge) -> str:
    return (
        f"{edge.source_repo} -> {edge.target_repo} "
        f"({edge.scope.value} dependency on {edge.package.ecosystem.value} package "
        f"'{edge.package.name}', declared in {edge.source_repo}/{edge.manifest_path})"
    )


def _guarded(operation: Callable[[], ToolOutput]) -> ToolOutput:
    try:
        return operation()
    except AnalyzerError as exc:
        return f"Error: {exc}", []


def build_graph_tools(workspace: Workspace) -> list[BaseTool]:
    """Tools for the hub: the workspace inventory and the dependency graph."""

    @tool(response_format="content_and_artifact")
    def list_repositories() -> ToolOutput:
        """List the indexed repositories and the dependency edges declared between them."""
        current = workspace.state
        if not current.repositories:
            return "No repositories are indexed yet.", []
        lines = ["Indexed repositories:"]
        lines += [
            f"- {r.name} ({r.source}, commit {(r.commit or 'unknown')[:12]}, {r.file_count} files)"
            for r in current.repositories
        ]
        lines.append("")
        lines.append("Declared dependency edges (dependent -> dependency):")
        lines += [f"- {_describe_edge(e)}" for e in current.graph.edges] or ["- none found"]
        return "\n".join(lines), []

    @tool(response_format="content_and_artifact")
    def analyze_impact(repo: str) -> ToolOutput:
        """Report which indexed repositories are affected if `repo` fails or breaks.

        Returns every direct and transitive dependent with the dependency chain that
        links it to `repo`, based on package manifests. Chains that include an
        optional or development-only dependency are marked as weak.
        """

        def run() -> ToolOutput:
            current = workspace.state
            impacted = current.graph.impact_of(repo)
            if not impacted:
                return (
                    f"No indexed repository declares a dependency on {repo}, directly or "
                    "transitively. Undeclared runtime coupling is not covered by this check.",
                    [],
                )
            lines = [f"If {repo} fails, {len(impacted)} indexed repositories are affected:"]
            sources: list[Source] = []
            for item in impacted:
                strength = "weak: optional/dev dependency on the path" if item.weak else "runtime"
                lines.append(f"\n{item.repo} (distance {item.distance}, {strength})")
                for edge in item.path:
                    lines.append(f"  {_describe_edge(edge)}")
                    sources.append(_source(edge.source_repo, edge.manifest_path, 1, 1))
            return "\n".join(lines), sources

        return _guarded(run)

    return [list_repositories, analyze_impact]


def build_rag_repository_tools(
    settings: Settings, store: VectorStore, workspace: Workspace, repo: str
) -> list[BaseTool]:
    """Expert tools for ``repo`` in rag mode: vector search plus files on disk."""

    @tool(response_format="content_and_artifact")
    def search_code(query: str) -> ToolOutput:
        """Semantic search over this repository's code and documentation.

        Args:
            query: A natural-language description of what you are looking for.
        """

        def run() -> ToolOutput:
            workspace.state.repository(repo)
            hits = store.search(query, top_k=settings.retrieval_top_k, repo=repo)
            if not hits:
                return "No results.", []
            blocks = [
                f"[{hit.chunk.citation}] (similarity {hit.score:.2f})\n{hit.chunk.text}"
                for hit in hits
            ]
            sources = [
                _source(h.chunk.repo, h.chunk.path, h.chunk.start_line, h.chunk.end_line)
                for h in hits
            ]
            return "\n\n---\n\n".join(blocks), sources

        return _guarded(run)

    @tool(response_format="content_and_artifact")
    def find_references(text: str) -> ToolOutput:
        """Find lines in this repository containing an exact, case-sensitive piece of text.

        Args:
            text: The literal text to look for, such as `import markupsafe` or a function name.
        """

        def run() -> ToolOutput:
            if not text.strip():
                return "Error: text must not be empty", []
            target = workspace.state.repository(repo)
            lines: list[str] = []
            sources: list[Source] = []
            total = 0
            for file in scan_repository(
                target.name, Path(target.root), max_file_bytes=settings.max_file_bytes
            ):
                for number, line in enumerate(file.text.splitlines(), start=1):
                    if text not in line:
                        continue
                    total += 1
                    if len(lines) < MAX_REFERENCE_HITS:
                        snippet = line.strip()[:MAX_REFERENCE_LINE_CHARS]
                        lines.append(f"{file.repo}/{file.path}:{number}: {snippet}")
                        sources.append(_source(file.repo, file.path, number, number))
            if not lines:
                return f"No lines contain {text!r}.", []
            header = f"{total} matching lines"
            if total > len(lines):
                header += f" (showing the first {len(lines)}; use a more specific text)"
            return header + ":\n" + "\n".join(lines), sources

        return _guarded(run)

    @tool(response_format="content_and_artifact")
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> ToolOutput:
        """Read part of a file from this repository, with line numbers.

        Args:
            path: File path relative to the repository root, as shown in search results.
            start_line: First line to return (1-based).
            end_line: Last line to return (inclusive). At most 400 lines are returned per call.
        """

        def run() -> ToolOutput:
            root = Path(workspace.state.repository(repo).root).resolve()
            target = (root / path).resolve()
            # Same rules as indexing: stay inside the checkout, and never serve a
            # file type the scanner would have refused (secrets, binaries, lockfiles).
            if not target.is_relative_to(root) or not target.is_file():
                return f"Error: no such file {path!r} in {repo}", []
            relative = target.relative_to(root)
            if language_for(PurePosixPath(relative.as_posix())) is None:
                return f"Error: {path!r} is not an indexed file type", []
            if target.stat().st_size > settings.max_file_bytes:
                return f"Error: {path!r} is too large to read", []
            try:
                all_lines = target.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                return f"Error: {path!r} could not be read as text", []

            first = max(start_line, 1)
            last = min(end_line or len(all_lines), len(all_lines), first + MAX_READ_LINES - 1)
            if first > len(all_lines):
                return f"Error: {path!r} has only {len(all_lines)} lines", []
            body = "\n".join(
                f"{number:>5}  {all_lines[number - 1]}" for number in range(first, last + 1)
            )
            header = f"{repo}/{relative.as_posix()} lines {first}-{last} of {len(all_lines)}"
            return f"{header}\n{body}", [_source(repo, relative.as_posix(), first, last)]

        return _guarded(run)

    return [search_code, find_references, read_file]


# --- json mode: the graph is the only source ----------------------------------

MAX_MAP_CHARS_FLOOR = 2_000


def build_usage_tool(workspace: Workspace, knowledge: KnowledgeStore) -> BaseTool:
    """Hub tool (json mode): where other repositories use a repository's code."""

    @tool(response_format="content_and_artifact")
    def find_usages(repo: str, symbol: str | None = None) -> ToolOutput:
        """List where other indexed repositories import code defined in `repo`.

        Computed from the import statements in every indexed Python file, so it is
        exact for static imports and silent about dynamic ones. Each line gives the
        importing file and line, the name imported, and where that name is defined.

        Args:
            repo: The repository whose definitions you are asking about.
            symbol: Only report uses of this class, function or variable name.
        """

        def run() -> ToolOutput:
            workspace.state.repository(repo)
            graph = knowledge.graph()
            nodes = {node["id"]: node for node in graph["nodes"]}
            relevant = [
                (edge, nodes[edge["source"]], nodes[edge["target"]])
                for edge in graph["edges"]
                if edge["kind"] in ("uses", "imports")
            ]
            relevant = [
                (edge, user, target)
                for edge, user, target in relevant
                if target["repo"] == repo and user["repo"] != repo
            ]
            # A line that imports a name is reported once, as the name it uses; the
            # bare "imports module" form is kept for ``import pkg`` style statements.
            named = {
                (edge["source"], edge["line"]) for edge, _, _ in relevant if edge["kind"] == "uses"
            }

            lines: list[str] = []
            sources: list[Source] = []
            per_repo: dict[str, int] = {}
            for edge, user, target in relevant:
                if edge["kind"] == "uses":
                    if symbol is not None and target["name"] != symbol:
                        continue
                    what = (
                        f"uses {target['name']} "
                        f"(defined at {target['repo']}/{target['path']}:{target['line']})"
                    )
                else:
                    if symbol is not None or (edge["source"], edge["line"]) in named:
                        continue
                    what = f"imports module {target['repo']}/{target['path']}"
                per_repo[user["repo"]] = per_repo.get(user["repo"], 0) + 1
                if len(lines) < MAX_USAGE_LINES:
                    lines.append(f"{user['repo']}/{user['path']}:{edge['line']} {what}")
                    sources.append(_source(user["repo"], user["path"], edge["line"], edge["line"]))
            subject = f"{symbol} from {repo}" if symbol else f"code from {repo}"
            if not lines:
                return (
                    f"No other indexed repository statically imports {subject}. "
                    "Only Python imports are tracked.",
                    [],
                )
            summary = ", ".join(f"{name}: {count}" for name, count in sorted(per_repo.items()))
            header = f"Static imports of {subject} by other repositories ({summary})"
            total = sum(per_repo.values())
            if total > len(lines):
                header += f"; showing the first {len(lines)} of {total}, pass symbol to narrow"
            return header + ":\n" + "\n".join(sorted(lines)), sources

        return _guarded(run)

    return find_usages


class _RepoGraph:
    """A read-only view of one repository's slice of the workspace graph."""

    def __init__(self, knowledge: KnowledgeStore, repo: str) -> None:
        graph = knowledge.graph()
        self.repo = repo
        self.nodes: dict[str, dict[str, Any]] = {n["id"]: n for n in graph["nodes"]}
        self._edges: list[dict[str, Any]] = graph["edges"]

    def files(self) -> list[dict[str, Any]]:
        return sorted(
            (n for n in self.nodes.values() if n["kind"] == "file" and n["repo"] == self.repo),
            key=lambda n: n["path"],
        )

    def symbols(self) -> list[dict[str, Any]]:
        return [n for n in self.nodes.values() if n["kind"] == "symbol" and n["repo"] == self.repo]

    def defined_in(self) -> dict[str, list[dict[str, Any]]]:
        """Map each file id to the symbol nodes it defines, ordered by line."""
        by_file: dict[str, list[dict[str, Any]]] = {}
        for edge in self._edges:
            if edge["kind"] == "defines" and self.nodes[edge["source"]]["repo"] == self.repo:
                by_file.setdefault(edge["source"], []).append(self.nodes[edge["target"]])
        for symbols in by_file.values():
            symbols.sort(key=lambda s: s["line"])
        return by_file

    def edges(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self._edges if e["kind"] == kind]


def build_json_repository_tools(
    settings: Settings, workspace: Workspace, knowledge: KnowledgeStore, repo: str
) -> list[BaseTool]:
    """Expert tools for ``repo`` in json mode: read only this repository's graph.

    Everything comes from the workspace graph; there is no file content to read in
    this mode, so the tools report structure (files, definitions) and the import
    links between them, with the line numbers to cite.
    """

    def graph() -> _RepoGraph:
        workspace.state.repository(repo)  # raises on an unknown repository
        return _RepoGraph(knowledge, repo)

    @tool(response_format="content_and_artifact")
    def repo_map(path_prefix: str = "") -> ToolOutput:
        """Show this repository's structure: each file and the top-level names it defines.

        Start here to find where something lives. Lists classes, functions and
        module-level variables with their line numbers (not their bodies or signatures,
        which this mode does not store). A large repository is shown as files only until
        you narrow it with a path prefix.

        Args:
            path_prefix: Only list files whose path starts with this, e.g. `src/flask/json`.
        """

        def run() -> ToolOutput:
            view = graph()
            files = [f for f in view.files() if f["path"].startswith(path_prefix)]
            if not files:
                return f"No files under {path_prefix!r} in {repo}.", []
            defined = view.defined_in()
            scope = f" under {path_prefix}" if path_prefix else ""
            header = f"{repo}: {len(files)} files{scope}"
            budget = max(settings.map_max_chars, MAX_MAP_CHARS_FLOOR)

            detailed: list[str] = []
            for file in files:
                symbols = defined.get(file["id"], [])
                detailed.append(f"{file['path']}  ({len(symbols)} definitions)")
                detailed += [f"  {s['symbol_kind']} {s['name']}  :{s['line']}" for s in symbols]
            body = "\n".join(detailed)
            if len(body) <= budget:
                return f"{header}\n\n{body}", []

            # Too large to show every definition; fall back to a file listing.
            files_only = "\n".join(
                f"{f['path']}  ({len(defined.get(f['id'], []))} definitions)" for f in files
            )
            note = "Too many definitions to list; showing files only. Pass path_prefix for one."
            return f"{header}\n{note}\n\n{files_only}", []

        return _guarded(run)

    @tool(response_format="content_and_artifact")
    def find_symbol(name: str) -> ToolOutput:
        """Find where a class, function or module-level name is defined in this repository.

        Also lists the files in this repository that import it.

        Args:
            name: The name to look for, e.g. `Markup` or `escape`. Case-insensitive;
                matches part of a name if there is no exact match.
        """

        def run() -> ToolOutput:
            view = graph()
            wanted = name.strip().lower()
            if not wanted:
                return "Error: name must not be empty", []
            symbols = view.symbols()
            matches = [s for s in symbols if s["name"].lower() == wanted]
            if not matches:
                matches = [s for s in symbols if wanted in s["name"].lower()]
            if not matches:
                return f"No definition matching {name!r} in {repo}.", []

            importers: dict[str, list[str]] = {}
            for edge in view.edges("uses"):
                user = view.nodes[edge["source"]]
                if user["repo"] == repo:
                    importers.setdefault(edge["target"], []).append(
                        f"{repo}/{user['path']}:{edge['line']}"
                    )

            matches.sort(key=lambda s: (s["path"], s["line"]))
            lines = [f"{len(matches)} definitions matching {name!r} in {repo}:"]
            sources: list[Source] = []
            for symbol in matches[:MAX_SYMBOL_MATCHES]:
                lines.append(
                    f"\n{symbol['symbol_kind']} {symbol['name']} "
                    f"— {repo}/{symbol['path']}:{symbol['line']}"
                )
                sources.append(_source(repo, symbol["path"], symbol["line"], symbol["line"]))
                used_by = importers.get(symbol["id"], [])
                if used_by:
                    shown = ", ".join(sorted(used_by)[:15])
                    more = f" and {len(used_by) - 15} more" if len(used_by) > 15 else ""
                    lines.append(f"  imported in this repository by: {shown}{more}")
            if len(matches) > MAX_SYMBOL_MATCHES:
                lines.append(f"\n… {len(matches) - MAX_SYMBOL_MATCHES} more; use a longer name")
            return "\n".join(lines), sources

        return _guarded(run)

    @tool(response_format="content_and_artifact")
    def list_dependencies(path: str) -> ToolOutput:
        """List what one file in this repository imports from indexed code.

        Shows the modules and named symbols the file imports that resolve to a file in
        this or another indexed repository. Imports of the standard library or of
        packages that are not indexed are not tracked, so they do not appear.

        Args:
            path: A file path in this repository, as shown by repo_map.
        """

        def run() -> ToolOutput:
            view = graph()
            file_id = f"file:{repo}/{path.strip().lstrip('./')}"
            if file_id not in view.nodes:
                return f"Error: no such file {path!r} in {repo}; see repo_map for paths", []

            lines: list[str] = []
            sources: list[Source] = []
            for edge in view.edges("uses"):
                if edge["source"] != file_id:
                    continue
                target = view.nodes[edge["target"]]
                lines.append(
                    f"{repo}/{path}:{edge['line']} uses {target['name']} "
                    f"(defined at {target['repo']}/{target['path']}:{target['line']})"
                )
                line = target["line"]
                sources.append(_source(target["repo"], target["path"], line, line))
            for edge in view.edges("imports"):
                if edge["source"] != file_id:
                    continue
                target = view.nodes[edge["target"]]
                lines.append(
                    f"{repo}/{path}:{edge['line']} imports module {target['repo']}/{target['path']}"
                )
            if not lines:
                return (
                    f"{repo}/{path} imports nothing from indexed code (standard-library and "
                    "third-party imports are not tracked).",
                    [],
                )
            return f"{path} depends on:\n" + "\n".join(sorted(lines)), sources

        return _guarded(run)

    return [repo_map, find_symbol, list_dependencies]
