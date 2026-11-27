"""A LangChain agent that reads a cloned repository directly from disk.

This is a second analysis mode, next to the Chat tab agent. That one first builds a
Ctags JSON index (and can use a Neo4j graph). This one does not. It only needs the
clone folder, then lists, reads and searches the files there.

Flow:
  1. Resolve ``repo`` to a local folder (reuse an existing checkout, or clone).
  2. Give Claude four file tools, all sandboxed to that folder.
  3. Return the final answer and the tool calls the model made.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.config import Settings
from repo_analyzer.domain import RepoSnapshot
from repo_analyzer.errors import (
    InvalidRepositorySourceError,
    RepositoryFetchError,
    UnknownRepositoryError,
)
from repo_analyzer.ingestion.fetcher import LOCAL_OWNER, RepoFetcher
from repo_analyzer.ingestion.scanner import IGNORED_DIRECTORIES, language_for
from repo_analyzer.workspace import Workspace

# Caps so one tool call cannot flood the model's context.
MAX_READ_LINES = 400
MAX_SEARCH_HITS = 60
MAX_SEARCH_LINE_CHARS = 240
MAX_LIST_FILES = 500
MAX_TREE_ENTRIES = 400
MAX_TREE_DEPTH = 5
MAX_OUTPUT_CHARS = 40_000
MAX_REGEX_LENGTH = 300
MAX_REGEX_SUBJECT_CHARS = 2_000

DIRECT_SYSTEM_PROMPT = """\
You analyze one cloned repository by reading its files directly.
There is no index, no search database and no code graph.

Tools:
- repo_tree(): a short file tree of the repository
- list_files(path="."): source and documentation files under a path
- read_file(path, start_line=1, end_line=None): read a text file, with line numbers
- search_code(pattern, glob=None): search file lines for a Python regex (plain text \
works when it has no special characters)

Start with repo_tree or list_files if you do not yet know the layout. Search for the \
names the code would use, then read the files that support each claim. Cite paths as \
path:line. If you cannot find something, say so.
"""


@dataclass(frozen=True, slots=True)
class DirectAskResult:
    """The agent's answer to one question about one repository."""

    repo: str
    question: str
    answer: str
    tool_calls: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "question": self.question,
            "answer": self.answer,
            "tool_calls": self.tool_calls,
        }


# ---------------------------------------------------------------------------
# Path sandbox
# ---------------------------------------------------------------------------


def _normalize_relative(path: str) -> str | None:
    """Turn a tool path into a relative POSIX path, or ``None`` if it is absolute."""
    raw = path.strip().replace("\\", "/")
    if not raw or raw == ".":
        return ""
    # Reject Windows drive paths and UNC paths even when we are running on Linux.
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        return None
    posix = PurePosixPath(raw)
    if posix.is_absolute():
        return None
    return "" if posix.as_posix() == "." else posix.as_posix().lstrip("/")


def resolve_under_root(root: Path, path: str) -> Path | str:
    """Resolve ``path`` inside ``root``, or return an ``Error: ...`` message.

    The tools call this before any read, so ``../``, absolute paths and symlinks
    that point outside the clone are rejected.
    """
    relative = _normalize_relative(path)
    if relative is None:
        return "Error: path must be relative to the repository root"
    if any(part in IGNORED_DIRECTORIES for part in PurePosixPath(relative).parts):
        return f"Error: {path!r} is in a skipped directory"
    base = root.resolve()
    # Join the relative parts only (never ``root / "/etc/passwd"``, which would
    # discard ``root`` on POSIX).
    candidate = (base.joinpath(*PurePosixPath(relative).parts) if relative else base).resolve()
    if not candidate.is_relative_to(base):
        return "Error: path escapes the repository"
    return candidate


def _relative_posix(root: Path, absolute: Path) -> str:
    return absolute.resolve().relative_to(root.resolve()).as_posix()


def _is_skipped_dir(name: str) -> bool:
    return name in IGNORED_DIRECTORIES


def _readable_file(path: Path, *, max_file_bytes: int) -> str | None:
    """Return a UTF-8 text file's contents, or ``None`` to skip it.

    Symlinks, binaries, secrets, unknown types and oversized files are skipped,
    same idea as the indexer, but the result is never written to a JSON file.
    """
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > max_file_bytes:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text


def _read_allowed_file(root: Path, absolute: Path, *, max_file_bytes: int) -> str | None:
    if absolute.is_symlink() or not absolute.is_file():
        return None
    rel = PurePosixPath(_relative_posix(root, absolute))
    if language_for(rel) is None:
        return None
    if any(part in IGNORED_DIRECTORIES for part in rel.parts):
        return None
    return _readable_file(absolute, max_file_bytes=max_file_bytes)


def _iter_source_files(
    root: Path, *, max_file_bytes: int, under: str = ""
) -> list[tuple[str, str]]:
    """Source/docs files under ``root`` (optionally under ``under``), as (relative, text)."""
    base = root.resolve()
    start = base.joinpath(*PurePosixPath(under).parts) if under else base
    collected: list[tuple[str, str]] = []
    if not start.exists():
        return collected
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not _is_skipped_dir(d))
        for filename in sorted(filenames):
            absolute = Path(dirpath, filename)
            try:
                relative = _relative_posix(base, absolute)
            except ValueError:
                continue
            text = _read_allowed_file(base, absolute, max_file_bytes=max_file_bytes)
            if text is None:
                continue
            collected.append((relative, text))
    return collected


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated to {limit} characters]"


def _glob_match(relative: str, pattern: str) -> bool:
    pat = pattern.strip().replace("\\", "/").lstrip("./")
    if not pat:
        return True
    path = PurePosixPath(relative)
    return path.match(pat) or fnmatch.fnmatch(relative, pat) or fnmatch.fnmatch(path.name, pat)


# ---------------------------------------------------------------------------
# Tools (closed over the clone root)
# ---------------------------------------------------------------------------


def build_direct_tools(root: Path, *, max_file_bytes: int) -> list[BaseTool]:
    """Four file tools, all restricted to ``root``."""

    root = root.resolve()

    @tool
    def repo_tree() -> str:
        """A short file tree of the repository (skipped folders such as .git are omitted)."""
        lines: list[str] = [f"{root.name}/"]
        count = 0
        truncated = False

        def walk(current: Path, prefix: str, depth: int) -> None:
            nonlocal count, truncated
            if truncated or depth > MAX_TREE_DEPTH:
                return
            try:
                entries = sorted(current.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            except OSError as exc:
                lines.append(f"{prefix}[cannot list: {exc}]")
                return
            visible = [
                entry
                for entry in entries
                if not entry.is_symlink()
                and not _is_skipped_dir(entry.name)
                and (entry.is_dir() or language_for(PurePosixPath(entry.name)) is not None)
            ]
            for index, entry in enumerate(visible):
                if count >= MAX_TREE_ENTRIES:
                    truncated = True
                    lines.append(f"{prefix}... (tree truncated)")
                    return
                last = index == len(visible) - 1
                branch = "└── " if last else "├── "
                child_prefix = prefix + ("    " if last else "│   ")
                if entry.is_dir():
                    lines.append(f"{prefix}{branch}{entry.name}/")
                    count += 1
                    walk(entry, child_prefix, depth + 1)
                else:
                    lines.append(f"{prefix}{branch}{entry.name}")
                    count += 1

        walk(root, "", 1)
        return _truncate("\n".join(lines))

    @tool
    def list_files(path: str = ".") -> str:
        """List source and documentation files under a directory (default: the repo root).

        Args:
            path: Directory relative to the repository root.
        """
        target = resolve_under_root(root, path)
        if isinstance(target, str):
            return target
        if not target.exists():
            return f"Error: no such path {path!r}"
        if target.is_file():
            if _read_allowed_file(root, target, max_file_bytes=max_file_bytes) is None:
                return f"Error: {path!r} is not a readable source file"
            return _relative_posix(root, target)
        files = _iter_source_files(
            root, max_file_bytes=max_file_bytes, under=_relative_posix(root, target)
        )
        if not files:
            return f"No readable source files under {path!r}."
        return _truncate(_list_files_text(path, files))

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """Read part of a text file, with line numbers.

        Args:
            path: File path relative to the repository root.
            start_line: First line to return (1-based).
            end_line: Last line to return (inclusive). At most 400 lines per call.
        """
        target = resolve_under_root(root, path)
        if isinstance(target, str):
            return target
        if not target.is_file() or target.is_symlink():
            return f"Error: no such file {path!r}"
        text = _read_allowed_file(root, target, max_file_bytes=max_file_bytes)
        if text is None:
            relative = PurePosixPath(_relative_posix(root, target))
            if target.is_file() and language_for(relative) is None:
                return f"Error: {path!r} is not a readable source file"
            if target.is_file() and target.stat().st_size > max_file_bytes:
                return f"Error: {path!r} is too large to read"
            return f"Error: {path!r} could not be read as text"
        all_lines = text.splitlines()
        first = max(start_line, 1)
        last = min(end_line or len(all_lines), len(all_lines), first + MAX_READ_LINES - 1)
        if first > len(all_lines):
            return f"Error: {path!r} has only {len(all_lines)} lines"
        rel = _relative_posix(root, target)
        body = "\n".join(
            f"{number:>5}  {all_lines[number - 1]}" for number in range(first, last + 1)
        )
        header = f"{rel} lines {first}-{last} of {len(all_lines)}"
        return _truncate(f"{header}\n{body}")

    @tool
    def search_code(pattern: str, glob: str | None = None) -> str:
        """Search the repository for lines matching a Python regular expression.

        Plain text works when it has no regex special characters (for example an
        identifier or a route string). Use `glob` to limit which files are searched,
        for example `*.py` or `src/**/*.ts`.

        Args:
            pattern: Python regular expression, matched against each line.
            glob: Optional filename pattern (fnmatch / glob).
        """
        if not pattern:
            return "Error: pattern must not be empty"
        if len(pattern) > MAX_REGEX_LENGTH:
            return f"Error: pattern is longer than {MAX_REGEX_LENGTH} characters"
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"Error: invalid regular expression: {exc}"

        lines: list[str] = []
        total = 0
        for relative, text in _iter_source_files(root, max_file_bytes=max_file_bytes):
            if glob and not _glob_match(relative, glob):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line[:MAX_REGEX_SUBJECT_CHARS]) is None:
                    continue
                total += 1
                if len(lines) < MAX_SEARCH_HITS:
                    snippet = line.strip()[:MAX_SEARCH_LINE_CHARS]
                    lines.append(f"{relative}:{number}: {snippet}")
        if not lines:
            return f"No lines match {pattern!r}."
        header = f"{total} matching lines"
        if total > len(lines):
            header += f" (showing the first {len(lines)}; narrow the pattern or glob)"
        return _truncate(header + ":\n" + "\n".join(lines))

    return [repo_tree, list_files, read_file, search_code]


def _list_files_text(path: str, files: list[tuple[str, str]]) -> str:
    shown = files[:MAX_LIST_FILES]
    lines = [f"{len(files)} files under {path}"]
    lines += [f"{rel} ({len(text.splitlines())} lines)" for rel, text in shown]
    if len(files) > len(shown):
        lines.append(f"... {len(files) - len(shown)} more; narrow with `path`")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resolve a repo argument to a local checkout
# ---------------------------------------------------------------------------


def resolve_checkout(
    repo: str,
    fetcher: RepoFetcher,
    workspace: Workspace | None = None,
) -> RepoSnapshot:
    """Return a local folder for ``repo`` without using any index JSON.

    ``repo`` may be a git URL, a local directory (if local paths are allowed),
    the name of an already-cloned checkout, or the name of an indexed repository
    (only its checkout path is used, not its JSON index).
    """
    source = repo.strip()
    if not source:
        raise InvalidRepositorySourceError("A repository URL or name is required")

    if _is_url(source) or _looks_like_path(source):
        return fetcher.ensure(source)

    # A short name: an existing checkout, or a workspace entry's checkout path.
    if workspace is not None:
        for item in workspace.state.repositories:
            if item.name == source or item.source == source:
                root = Path(item.root)
                if not root.is_dir():
                    raise RepositoryFetchError(f"The checkout for {item.name!r} is missing: {root}")
                return RepoSnapshot(
                    name=item.name,
                    owner=item.owner,
                    source=item.source,
                    root=root,
                    commit=item.commit,
                    default_branch=item.default_branch,
                )

    existing = fetcher.existing_checkout(source)
    if existing is not None:
        return RepoSnapshot(
            name=source,
            owner=LOCAL_OWNER,
            source=str(existing),
            root=existing,
            commit=None,
            default_branch=None,
        )

    raise UnknownRepositoryError(
        f"No checkout named {source!r}. Pass a git URL (or a local path), "
        "or clone it first."
    )


def _is_url(source: str) -> bool:
    return "://" in source


def _looks_like_path(source: str) -> bool:
    if _is_url(source):
        return False
    return (
        "/" in source
        or "\\" in source
        or source.startswith(".")
        or (len(source) >= 2 and source[1] == ":")
    )


# ---------------------------------------------------------------------------
# Run the agent
# ---------------------------------------------------------------------------


def _message_text(content: Any) -> str:
    """Turn LangChain message content (a string or a list of parts) into text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(str(block["text"]))
            elif hasattr(block, "text"):
                parts.append(str(block.text))
        return " ".join(parts).strip()
    return str(content)


def parse_agent_result(result: dict[str, Any], *, repo: str, question: str) -> DirectAskResult:
    """Read the agent message list and pick out tool calls, then the final answer."""
    tool_calls: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    answer = ""

    for message in result.get("messages") or []:
        for tool_call in getattr(message, "tool_calls", None) or []:
            if isinstance(tool_call, dict):
                name = tool_call.get("name") or ""
                args = dict(tool_call.get("args") or tool_call.get("arguments") or {})
                call_id = tool_call.get("id")
            else:
                name = getattr(tool_call, "name", "") or ""
                args = dict(getattr(tool_call, "args", None) or {})
                call_id = getattr(tool_call, "id", None)
            entry = {"name": name, "arguments": args, "result": None}
            tool_calls.append(entry)
            if call_id:
                pending[str(call_id)] = entry

        kind = type(message).__name__
        if kind == "ToolMessage" or getattr(message, "type", "") == "tool":
            text = _message_text(getattr(message, "content", ""))
            call_id = getattr(message, "tool_call_id", None)
            if call_id and str(call_id) in pending:
                pending[str(call_id)]["result"] = text
            continue

        text = _message_text(getattr(message, "content", ""))
        if kind == "AIMessage" and text:
            answer = text

    return DirectAskResult(repo=repo, question=question, answer=answer, tool_calls=tool_calls)


async def ask_direct(
    repo: str,
    question: str,
    *,
    settings: Settings,
    fetcher: RepoFetcher,
    workspace: Workspace | None = None,
    model: BaseChatModel | None = None,
) -> DirectAskResult:
    """Clone or reuse ``repo``, run the LangChain agent, and return its answer.

    ``model`` is injected by tests (a scripted fake). In production we reuse
    :func:`build_chat_model`, the same Claude settings as the Chat tab.
    """
    snapshot = resolve_checkout(repo, fetcher, workspace)
    chat_model = model if model is not None else build_chat_model(settings)
    tools = build_direct_tools(snapshot.root, max_file_bytes=settings.max_file_bytes)
    agent = create_agent(chat_model, tools, system_prompt=DIRECT_SYSTEM_PROMPT)
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": settings.agent_recursion_limit},
    )
    return parse_agent_result(result, repo=snapshot.name, question=question)
