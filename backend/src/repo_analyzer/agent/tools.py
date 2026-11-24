"""Tools the agent uses to inspect the indexed repositories.

Tools are built by a factory that closes over their dependencies, so there is no
module-level state and tests can build them against a temporary workspace.

Each tool returns ``(content, artifact)``: ``content`` is the text the model
reads; ``artifact`` is a list of structured source references that the API
streams to the UI and that never enters the model's context.

Expected failures (unknown repository, bad path, bad regex) are returned to the
model as ordinary text so it can correct itself and retry.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from langchain_core.tools import BaseTool, tool

from repo_analyzer.agent.skills import SkillLibrary
from repo_analyzer.config import Settings
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.ingestion.graph_store import MAX_NEIGHBOURS, GraphStore
from repo_analyzer.ingestion.metadata import MetadataStore
from repo_analyzer.ingestion.repo_index import RepoIndex, RepoIndexStore
from repo_analyzer.ingestion.scanner import language_for, scan_repository
from repo_analyzer.workspace import Workspace, WorkspaceState

MAX_READ_LINES = 400
MAX_SEARCH_HITS = 60
MAX_SEARCH_LINE_CHARS = 240
MAX_REGEX_LENGTH = 300
MAX_REGEX_SUBJECT_CHARS = 2_000  # a line is truncated to this before a regex runs on it
MAX_INFO_CHARS = 40_000  # keeps one get_repository_info result comfortably inside the context
MAX_INFO_SOURCES = 20
MAX_LIST_FILES = 500

Source = dict[str, Any]
ToolOutput = tuple[str, list[Source]]

logger = logging.getLogger(__name__)


def _source(repo: str, path: str, start_line: int, end_line: int) -> Source:
    return {"repo": repo, "path": path, "start_line": start_line, "end_line": end_line}


def _normalize_prefix(path: str) -> str:
    """Turn ``./src/``, ``/src`` or ``src`` into ``src``; ``"."`` or ``""`` into ``""``."""
    cleaned = PurePosixPath(path.strip().replace("\\", "/")).as_posix().lstrip("/")
    return "" if cleaned == "." else cleaned


def _under(file_path: str, prefix: str) -> bool:
    return not prefix or file_path == prefix or file_path.startswith(prefix + "/")


def _compact(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def _render_repository_info(index: RepoIndex, prefix: str) -> tuple[str, int]:
    """Serialise ``index`` (optionally only files under ``prefix``), shrinking if too large.

    Returns the JSON text and the number of file entries shown. Shrinking first drops
    per-file symbols, then drops files from the end of the list; either way the
    result says so, so the model knows to narrow with ``path``.
    """
    files = [f for f in index["files"] if _under(f["path"], prefix)]
    view: dict[str, Any] = {key: value for key, value in index.items() if key != "files"}
    if prefix:
        view["path_filter"] = prefix
        view["files_matching"] = len(files)

    def render(entries: list[dict[str, Any]], note: str | None) -> str:
        extra = {"note": note} if note else {}
        return _compact({**view, **extra, "files": entries})

    text = render(files, None)
    if len(text) <= MAX_INFO_CHARS:
        return text, len(files)

    hint = "pass `path` (a directory or file) to see the symbols"
    without_symbols = [{k: v for k, v in f.items() if k != "symbols"} for f in files]
    text = render(without_symbols, f"Per-file symbols omitted to keep this short; {hint}.")
    if len(text) <= MAX_INFO_CHARS:
        return text, len(files)

    shown = len(without_symbols)
    while shown > 0:
        shown = shown * 3 // 4
        text = render(
            without_symbols[:shown],
            f"Symbols omitted and the file list cut to the first {shown} of {len(files)}; "
            "narrow it with `path`.",
        )
        if len(text) <= MAX_INFO_CHARS:
            break
    return text, shown


def build_tools(
    settings: Settings,
    index_store: RepoIndexStore,
    workspace: Workspace,
    skills: SkillLibrary | None = None,
    metadata_store: MetadataStore | None = None,
    graph_store: GraphStore | None = None,
    graph_ws: str = "",
) -> list[BaseTool]:
    """The chat agent's tools.

    ``metadata_store`` adds ``get_endpoint_metadata``; ``graph_store`` adds the code-graph
    tools ``trace_endpoint``, ``callers_of`` and ``callees_of`` for workspace ``graph_ws``.
    """
    library = skills if skills is not None else SkillLibrary.load(settings.skills_dir)

    def state() -> WorkspaceState:
        return workspace.state

    def guarded(operation: Callable[[], ToolOutput]) -> ToolOutput:
        try:
            return operation()
        except AnalyzerError as exc:
            return f"Error: {exc}", []

    @tool(response_format="content_and_artifact")
    def list_repositories() -> ToolOutput:
        """List the indexed repositories with their owner, branch, commit and size."""
        current = state()
        if not current.repositories:
            return "No repositories are indexed yet.", []
        lines = ["Indexed repositories:"]
        lines += [
            f"- {r.name} (owner {r.owner}, {r.source}, branch {r.default_branch or 'unknown'}, "
            f"commit {(r.commit or 'unknown')[:12]}): {r.file_count} files, "
            f"{r.symbol_count} functions/classes, {r.dependency_count} declared dependencies"
            for r in current.repositories
        ]
        return "\n".join(lines), []

    @tool(response_format="content_and_artifact")
    def get_repository_info(repo: str, path: str | None = None) -> ToolOutput:
        """Return a repository's JSON index: basic info, file tree, dependencies and symbols.

        The JSON has `name`, `owner`, `url`, `default_branch`, `commit`, `indexed_at`,
        `stats` (file/line counts and languages), `manifests`, `dependencies` (each with a
        `kind` of runtime, optional or dev, and the manifest that declares it), and `files`.
        Each file has `path`, `language`, `size_bytes`, `line_count` and, for source files,
        `symbols`: its `functions`, `classes` and `imports` with line numbers, taken from
        Universal Ctags. Import lists are only as complete as Ctags makes them, and are
        often empty for JavaScript and TypeScript.

        Args:
            repo: Repository name.
            path: Only include files under this directory (or this one file), for example
                `src/flask`. Use it on large repositories: without it the symbols may be
                left out to keep the result short.
        """

        def run() -> ToolOutput:
            repository = state().repository(repo)
            index = index_store.load(repository.index_file)
            prefix = _normalize_prefix(path) if path else ""
            if prefix and not any(_under(f["path"], prefix) for f in index["files"]):
                return f"Error: no indexed files under {path!r} in {repo}", []
            text, _ = _render_repository_info(index, prefix)
            sources = [_source(repo, m["path"], 1, 1) for m in index["manifests"]]
            return text, sources[:MAX_INFO_SOURCES]

        return guarded(run)

    @tool(response_format="content_and_artifact")
    def search_code(
        query: str,
        repo: str | None = None,
        regex: bool = False,
        ignore_case: bool = False,
        path: str | None = None,
    ) -> ToolOutput:
        """Search the checked-out files for lines containing text (or matching a regex).

        This is exact matching, not semantic search: use the words the code itself would
        use, such as an identifier, an import statement, a package name or a config key.
        Only files the index covers are searched (no secrets, lockfiles or binaries).

        Args:
            query: The literal text to look for, or a Python regular expression if `regex`.
            repo: Restrict the search to one repository name. Omit to search all.
            regex: Treat `query` as a regular expression, matched against each line.
            ignore_case: Match case-insensitively.
            path: Only search files under this directory (or this one file).
        """

        def run() -> ToolOutput:
            if not query.strip():
                return "Error: query must not be empty", []
            matches = _line_matcher(query, regex=regex, ignore_case=ignore_case)
            if isinstance(matches, str):
                return matches, []
            prefix = _normalize_prefix(path) if path else ""

            current = state()
            targets = [current.repository(repo)] if repo else list(current.repositories)
            lines: list[str] = []
            sources: list[Source] = []
            total = 0
            for target in targets:
                for file in scan_repository(
                    target.name, Path(target.root), max_file_bytes=settings.max_file_bytes
                ):
                    if not _under(file.path, prefix):
                        continue
                    for number, line in enumerate(file.text.splitlines(), start=1):
                        if not matches(line):
                            continue
                        total += 1
                        if len(lines) < MAX_SEARCH_HITS:
                            snippet = line.strip()[:MAX_SEARCH_LINE_CHARS]
                            lines.append(f"{file.repo}/{file.path}:{number}: {snippet}")
                            sources.append(_source(file.repo, file.path, number, number))
            if not lines:
                return f"No lines match {query!r}.", []
            header = f"{total} matching lines"
            if total > len(lines):
                header += f" (showing the first {len(lines)}; narrow the query, repo or path)"
            return header + ":\n" + "\n".join(lines), sources

        return guarded(run)

    @tool(response_format="content_and_artifact")
    def read_file(
        repo: str, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ToolOutput:
        """Read part of a file from an indexed repository, with line numbers.

        Args:
            repo: Repository name.
            path: File path relative to the repository root, as shown in the JSON index.
            start_line: First line to return (1-based).
            end_line: Last line to return (inclusive). At most 400 lines are returned per call.
        """

        def run() -> ToolOutput:
            root = Path(state().repository(repo).root).resolve()
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

        return guarded(run)

    @tool(response_format="content_and_artifact")
    def load_skill(name: str) -> ToolOutput:
        """Load the instructions for a named agent skill.

        Call this before answering when a skill in the catalogue fits the question.
        The result is the skill's markdown body (without frontmatter).

        Args:
            name: The skill name from the catalogue, for example `repo-navigation`.
        """
        available = ", ".join(library.names()) or "none"
        skill = library.get(name)
        if skill is None:
            return (
                f"Error: Unknown skill {name!r}. Available skills: {available}.",
                [],
            )
        return skill.body, []

    tools: list[BaseTool] = [
        list_repositories,
        get_repository_info,
        search_code,
        read_file,
        load_skill,
    ]
    if metadata_store is not None:
        tools.append(build_metadata_tool(metadata_store, workspace))
    if graph_store is not None:
        tools += build_graph_tools(graph_store, workspace, graph_ws)
    return tools


def _span(path: str, line: int, end_line: int | None) -> str:
    return f"{path}:{line}-{end_line}" if end_line and end_line != line else f"{path}:{line}"


def build_graph_tools(graph_store: GraphStore, workspace: Workspace, ws: str) -> list[BaseTool]:
    """``trace_endpoint``, ``callers_of`` and ``callees_of`` over the code graph.

    Every query is fixed, parameterised Cypher inside ``graph_store``; the model only
    supplies values (repository, method, path, function name), never query text.
    """

    def unknown_repo(repo: str) -> str | None:
        try:
            workspace.state.repository(repo)
        except AnalyzerError as exc:
            return f"Error: {exc}"
        return None

    def graph_error(exc: Exception) -> ToolOutput:
        logger.warning("Code graph query failed: %s: %s", type(exc).__name__, exc)
        return (
            f"Error: the code graph could not be queried ({type(exc).__name__}). "
            "Use search_code and read_file instead.",
            [],
        )

    @tool(response_format="content_and_artifact")
    def trace_endpoint(repo: str, method: str, path: str) -> ToolOutput:
        """Trace an HTTP endpoint from its route handler down to the database tables.

        Returns the handler and the functions it calls (directly or through other calls,
        up to 6 deep) in call order, each with its `repo/path:start-end` range, and the
        tables each function reads or writes. It comes from static analysis of the code:
        read the listed ranges with `read_file` to explain what each step does to the
        data, and follow any listed gaps with `search_code`.

        Args:
            repo: Repository name.
            method: HTTP method, for example `GET` or `POST`.
            path: The route as declared (`/orders/{order_id}`) or a concrete URL path
                (`/orders/42`).
        """
        if error := unknown_repo(repo):
            return error, []
        try:
            trace = graph_store.trace_endpoint(ws, repo, method, path)
            endpoints = graph_store.endpoints(ws, repo) if trace is None else []
        except Exception as exc:  # Neo4j down, driver missing, query timeout
            return graph_error(exc)
        if trace is None:
            if not endpoints:
                return (
                    f"No endpoints are in the code graph for {repo} (only Python FastAPI "
                    "routes are detected). Use search_code and read_file instead.",
                    [],
                )
            listing = "\n".join(f"- {e['method']} {e['path']}" for e in endpoints)
            return (
                f"No endpoint matches {method.upper()} {path} in {repo}. "
                f"Endpoints in the code graph:\n{listing}",
                [],
            )

        lines = [
            f"{trace.method} {trace.path} in {repo} (route declared at "
            f"{repo}/{trace.file}:{trace.line})",
            "Call path from the handler, depth-first in call order:",
        ]
        sources: list[Source] = []
        for step in trace.steps:
            indent = "  " * step.depth
            location = f"{repo}/{_span(step.path, step.line, step.end_line)}"
            if step.repeated:
                lines.append(f"{indent}{step.depth}. {step.function}  {location}  (see above)")
                continue
            how = ""
            if step.via is not None:
                verb = "passed as a function" if step.via == "reference" else "called"
                how = f"  [{verb} at line {step.call_line}]"
            lines.append(f"{indent}{step.depth}. {step.function}  {location}{how}")
            for table, kind, line in step.tables:
                action = "reads" if kind == "READS" else "writes"
                lines.append(f"{indent}     {action} table {table} (line {line})")
            sources.append(_source(repo, step.path, step.line, step.end_line or step.line))
        if trace.truncated:
            lines.append("... cut at 60 steps; use callees_of to continue from a function.")
        if not any(step.tables for step in trace.steps):
            lines.append(
                "No table access was found on this path: the data may come from a cache, "
                "an external API, or code the graph does not model. Check with search_code."
            )
        if trace.gaps:
            lines.append(
                "Gaps (calls that matched several functions; resolve them with search_code): "
                + ", ".join(trace.gaps)
            )
        else:
            lines.append(
                "Gaps: none recorded. Calls through variables, dependency injection or "
                "dynamic dispatch are not in the graph."
            )
        return "\n".join(lines), sources

    def neighbours(repo: str, function: str, direction: str) -> ToolOutput:
        if error := unknown_repo(repo):
            return error, []
        try:
            rows = graph_store.neighbours(ws, repo, function, direction)
        except Exception as exc:
            return graph_error(exc)
        label = "callers of" if direction == "in" else "functions called by"
        if not rows:
            return (
                f"No {label} {function!r} in the code graph for {repo}. Check the name "
                "(`name` or `Class.method`), or use search_code.",
                [],
            )
        targets = sorted({row["target"] for row in rows})
        lines = [f"{len(rows)} {label} {function} in {repo}:"]
        if len(targets) > 1:
            lines.append(f"(the name matched {len(targets)} functions: {', '.join(targets)})")
        sources: list[Source] = []
        for row in rows:
            where = (
                f"calls it at line {row['call_line']}"
                if direction == "in"
                else f"called at line {row['call_line']}"
            )
            if row.get("via") == "reference":
                where += ", passed as a function"
            lines.append(
                f"- {row['name']}  {repo}/{_span(row['path'], row['line'], row['end_line'])}"
                f"  ({where})"
            )
            sources.append(_source(repo, row["path"], row["line"], row["end_line"] or row["line"]))
        if len(rows) >= MAX_NEIGHBOURS:
            lines.append(f"(showing the first {MAX_NEIGHBOURS})")
        return "\n".join(lines), sources

    @tool(response_format="content_and_artifact")
    def callers_of(repo: str, function: str) -> ToolOutput:
        """List the functions that call a function, from the code graph.

        Args:
            repo: Repository name.
            function: Function name (`price_order`), method (`Repo.load`) or id
                (`app/services/pricing.py:price_order`).
        """
        return neighbours(repo, function, "in")

    @tool(response_format="content_and_artifact")
    def callees_of(repo: str, function: str) -> ToolOutput:
        """List the functions a function calls, from the code graph.

        Args:
            repo: Repository name.
            function: Function name (`create_order`), method (`Repo.load`) or id
                (`app/routers/orders.py:create_order`).
        """
        return neighbours(repo, function, "out")

    return [trace_endpoint, callers_of, callees_of]


def build_metadata_tool(metadata_store: MetadataStore, workspace: Workspace) -> BaseTool:
    """``get_endpoint_metadata``: the model-written data-flow notes for a repository."""

    @tool(response_format="content_and_artifact")
    def get_endpoint_metadata(repo: str) -> ToolOutput:
        """Return per-endpoint data-flow metadata for a repository, written at index time.

        For each HTTP endpoint (and each backend call a front end makes) it lists the
        handler and location, where the data comes from (`data_source`, `source_types`,
        `tables`), `caching`, `transformations`, `side_effects`, `external_calls` and
        `notes`. It was generated by a language model from the code, so treat it as a
        strong hint and cite its `location` fields.

        Args:
            repo: Repository name.
        """
        try:
            repository = workspace.state.repository(repo)
        except AnalyzerError as exc:
            return f"Error: {exc}", []
        metadata = metadata_store.load(repository.owner, repository.name)
        if metadata is None:
            return f"No endpoint metadata exists for {repo}.", []
        sources = []
        for entry in metadata.get("entries", []):
            path, _, line = str(entry.get("location", "")).rpartition(":")
            if path and line.isdigit():
                sources.append(_source(repo, path, int(line), int(line)))
        return _compact(metadata), sources[:MAX_INFO_SOURCES]

    return get_endpoint_metadata


def build_list_files_tool(settings: Settings, workspace: Workspace) -> BaseTool:
    """``list_files`` over the local checkouts, for agents that have no JSON index."""

    @tool(response_format="content_and_artifact")
    def list_files(repo: str, path: str | None = None) -> ToolOutput:
        """List the source, config and documentation files in a repository, with line counts.

        Args:
            repo: Repository name.
            path: Only list files under this directory.
        """
        try:
            repository = workspace.state.repository(repo)
        except AnalyzerError as exc:
            return f"Error: {exc}", []
        prefix = _normalize_prefix(path) if path else ""
        entries = [
            (file.path, len(file.text.splitlines()))
            for file in scan_repository(
                repository.name, Path(repository.root), max_file_bytes=settings.max_file_bytes
            )
            if _under(file.path, prefix)
        ]
        if not entries:
            return f"No files under {path or '/'!r} in {repo}.", []
        shown = entries[:MAX_LIST_FILES]
        lines = [f"{len(entries)} files in {repo}" + (f" under {prefix}" if prefix else "")]
        lines += [f"{p} ({n} lines)" for p, n in shown]
        if len(entries) > len(shown):
            lines.append(f"... {len(entries) - len(shown)} more; narrow with `path`")
        return "\n".join(lines), []

    return list_files


def _line_matcher(query: str, *, regex: bool, ignore_case: bool) -> Callable[[str], bool] | str:
    """Build a per-line predicate, or return an ``Error: ...`` message for a bad regex."""
    if not regex:
        if ignore_case:
            needle = query.lower()
            return lambda line: needle in line.lower()
        return lambda line: query in line
    if len(query) > MAX_REGEX_LENGTH:
        return f"Error: regex is longer than {MAX_REGEX_LENGTH} characters"
    try:
        pattern = re.compile(query, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        return f"Error: invalid regular expression: {exc}"
    return lambda line: pattern.search(line[:MAX_REGEX_SUBJECT_CHARS]) is not None
