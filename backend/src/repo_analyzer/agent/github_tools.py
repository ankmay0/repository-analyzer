"""Repository tools backed by the GitHub REST API instead of a local clone.

``list_files``, ``search_code`` and ``read_file`` have the same names and
arguments as the local tools, so the agent's guidance applies unchanged. Each
workspace repository is mapped to a GitHub repository, a ref (branch, tag or
commit) and an optional sub-directory, which lets a repository that lives inside
a larger one (for example ``benchmarks/fixtures/shop-api``) be read in place.

Calls made:

- ``GET /repos/{owner}/{repo}/git/trees/{ref}?recursive=1`` for the file list
  (once per repository, then cached);
- ``GET /repos/{owner}/{repo}/contents/{path}?ref={ref}`` with the raw media
  type to read a file (cached).

Search is exact-text grep over files fetched through the contents API, not
GitHub code search: code search only covers the default branch and does not do
substring matching, so its results would not be comparable with the local tool.
Only files the local scanner would index are listed, searched or read.

Authentication uses a token (``GITHUB_TOKEN``); without one GitHub allows only
60 requests an hour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from langchain_core.tools import BaseTool, tool

from repo_analyzer.agent.tools import (
    MAX_READ_LINES,
    MAX_SEARCH_HITS,
    MAX_SEARCH_LINE_CHARS,
    ToolOutput,
    _line_matcher,
    _normalize_prefix,
    _source,
    _under,
)
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.ingestion.scanner import IGNORED_DIRECTORIES, language_for

GITHUB_API_URL = "https://api.github.com"
MAX_SEARCH_FILES = 300
MAX_LIST_FILES = 500


class GitHubAccessError(AnalyzerError):
    """GitHub refused a request or a repository is not mapped."""


@dataclass(frozen=True, slots=True)
class GitHubSource:
    """Where a workspace repository lives on GitHub."""

    owner: str
    repo: str
    ref: str = "main"
    root: str = ""  # sub-directory inside the GitHub repository, "" for the top level

    def full_path(self, relative: str) -> str:
        return f"{self.root.strip('/')}/{relative}" if self.root.strip("/") else relative


@dataclass(slots=True)
class GitHubClient:
    """Small cached client for the two GitHub endpoints the tools need."""

    token: str | None = None
    base_url: str = GITHUB_API_URL
    max_file_bytes: int = 200_000
    transport: httpx.BaseTransport | None = None
    timeout: float = 20.0
    _trees: dict[GitHubSource, list[tuple[str, int]]] = field(default_factory=dict)
    _files: dict[tuple[GitHubSource, str], str] = field(default_factory=dict)
    _http: httpx.Client | None = None
    requests_made: int = 0

    def _client(self) -> httpx.Client:
        if self._http is None:
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repo-analyzer",
            }
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            self._http = httpx.Client(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
                transport=self.transport,
            )
        return self._http

    def _get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.requests_made += 1
        try:
            response = self._client().get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise GitHubAccessError(f"Could not reach GitHub: {exc}") from exc
        if response.status_code == 404:
            raise GitHubAccessError("Not found on GitHub")
        if response.status_code in (401, 403, 429):
            raise GitHubAccessError(
                f"GitHub refused the request (HTTP {response.status_code}); "
                "check GITHUB_TOKEN and the rate limit"
            )
        if response.status_code >= 300:
            raise GitHubAccessError(f"GitHub returned HTTP {response.status_code}")
        return response

    def files(self, source: GitHubSource) -> list[tuple[str, int]]:
        """``(path relative to source.root, size)`` for every indexable file."""
        if source not in self._trees:
            url = (
                f"/repos/{quote(source.owner)}/{quote(source.repo)}/git/trees/"
                f"{quote(source.ref, safe='')}"
            )
            data = self._get(url, params={"recursive": "1"}).json()
            prefix = source.root.strip("/")
            entries: list[tuple[str, int]] = []
            for item in data.get("tree", []):
                if item.get("type") != "blob":
                    continue
                full = str(item.get("path", ""))
                if prefix:
                    if not full.startswith(prefix + "/"):
                        continue
                    full = full[len(prefix) + 1 :]
                relative = PurePosixPath(full)
                if any(part in IGNORED_DIRECTORIES for part in relative.parts[:-1]):
                    continue
                if language_for(relative) is None:
                    continue
                size = int(item.get("size") or 0)
                if size > self.max_file_bytes:
                    continue
                entries.append((full, size))
            self._trees[source] = sorted(entries)
        return self._trees[source]

    def read(self, source: GitHubSource, relative: str) -> str:
        key = (source, relative)
        if key not in self._files:
            path = quote(source.full_path(relative))
            url = f"/repos/{quote(source.owner)}/{quote(source.repo)}/contents/{path}"
            response = self._get(
                url,
                params={"ref": source.ref},
                headers={"Accept": "application/vnd.github.raw+json"},
            )
            if len(response.content) > self.max_file_bytes:
                raise GitHubAccessError(f"{relative!r} is too large to read")
            try:
                self._files[key] = response.content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitHubAccessError(f"{relative!r} could not be read as text") from exc
        return self._files[key]

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None


def _clean_relative(path: str) -> str | None:
    """Normalise a user-supplied path; ``None`` if it tries to leave the repository."""
    cleaned = _normalize_prefix(path)
    parts = PurePosixPath(cleaned).parts
    if not cleaned or ".." in parts:
        return None
    return cleaned


def build_github_tools(client: GitHubClient, sources: dict[str, GitHubSource]) -> list[BaseTool]:
    """``list_files``, ``search_code`` and ``read_file`` reading from GitHub."""

    def source_for(repo: str) -> GitHubSource:
        if repo not in sources:
            known = ", ".join(sorted(sources)) or "none"
            raise GitHubAccessError(f"Unknown repository {repo!r}; known: {known}")
        return sources[repo]

    def guarded(operation: Callable[[], ToolOutput]) -> ToolOutput:
        try:
            return operation()
        except AnalyzerError as exc:
            return f"Error: {exc}", []

    @tool(response_format="content_and_artifact")
    def list_files(repo: str, path: str | None = None) -> ToolOutput:
        """List the source, config and documentation files in a repository, with sizes.

        Args:
            repo: Repository name.
            path: Only list files under this directory.
        """

        def run() -> ToolOutput:
            prefix = _normalize_prefix(path) if path else ""
            entries = [(p, s) for p, s in client.files(source_for(repo)) if _under(p, prefix)]
            if not entries:
                return f"No files under {path or '/'!r} in {repo}.", []
            shown = entries[:MAX_LIST_FILES]
            lines = [f"{len(entries)} files in {repo}" + (f" under {prefix}" if prefix else "")]
            lines += [f"{p} ({s} bytes)" for p, s in shown]
            if len(entries) > len(shown):
                lines.append(f"... {len(entries) - len(shown)} more; narrow with `path`")
            return "\n".join(lines), []

        return guarded(run)

    @tool(response_format="content_and_artifact")
    def search_code(
        query: str,
        repo: str | None = None,
        regex: bool = False,
        ignore_case: bool = False,
        path: str | None = None,
    ) -> ToolOutput:
        """Search the repository files on GitHub for lines containing text (or a regex).

        This is exact matching, not semantic search: use the words the code itself would
        use, such as an identifier, an import statement, a table name or a config key.

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
            names = [repo] if repo else sorted(sources)
            lines: list[str] = []
            refs = []
            total = 0
            for name in names:
                source = source_for(name)
                candidates = [p for p, _ in client.files(source) if _under(p, prefix)]
                for file_path in candidates[:MAX_SEARCH_FILES]:
                    text = client.read(source, file_path)
                    for number, line in enumerate(text.splitlines(), start=1):
                        if not matches(line):
                            continue
                        total += 1
                        if len(lines) < MAX_SEARCH_HITS:
                            snippet = line.strip()[:MAX_SEARCH_LINE_CHARS]
                            lines.append(f"{name}/{file_path}:{number}: {snippet}")
                            refs.append(_source(name, file_path, number, number))
            if not lines:
                return f"No lines match {query!r}.", []
            header = f"{total} matching lines"
            if total > len(lines):
                header += f" (showing the first {len(lines)}; narrow the query, repo or path)"
            return header + ":\n" + "\n".join(lines), refs

        return guarded(run)

    @tool(response_format="content_and_artifact")
    def read_file(
        repo: str, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ToolOutput:
        """Read part of a file from GitHub, with line numbers.

        Args:
            repo: Repository name.
            path: File path relative to the repository root, as shown in the JSON index.
            start_line: First line to return (1-based).
            end_line: Last line to return (inclusive). At most 400 lines are returned per call.
        """

        def run() -> ToolOutput:
            source = source_for(repo)
            relative = _clean_relative(path)
            known = {p for p, _ in client.files(source)}
            if relative is None or relative not in known:
                return f"Error: no such file {path!r} in {repo}", []
            all_lines = client.read(source, relative).splitlines()
            first = max(start_line, 1)
            if first > len(all_lines):
                return f"Error: {path!r} has only {len(all_lines)} lines", []
            last = min(end_line or len(all_lines), len(all_lines), first + MAX_READ_LINES - 1)
            body = "\n".join(
                f"{number:>5}  {all_lines[number - 1]}" for number in range(first, last + 1)
            )
            header = f"{repo}/{relative} lines {first}-{last} of {len(all_lines)}"
            return f"{header}\n{body}", [_source(repo, relative, first, last)]

        return guarded(run)

    return [list_files, search_code, read_file]
