"""Tools the agents use to inspect the indexed repositories.

There are two sets, matching the two kinds of agent:

* graph tools, for the hub: what is indexed and how the repositories depend on
  each other. They never return file contents.
* repository tools, for a repository expert: search and read the code of exactly
  one repository. The repository is fixed when the tools are built and is not a
  parameter, so an expert cannot be talked into reading another repository.

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
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace

MAX_READ_LINES = 400
MAX_REFERENCE_HITS = 60
MAX_REFERENCE_LINE_CHARS = 240

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


def build_repository_tools(
    settings: Settings, store: VectorStore, workspace: Workspace, repo: str
) -> list[BaseTool]:
    """Tools for the expert on ``repo``: search and read that repository only."""

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
