"""Split source files into overlapping line windows.

Chunks are line-based (not token- or AST-based) for two reasons: every chunk
maps to an exact ``path:start-end`` range that an answer can cite and a reader
can open, and the same code handles every language. Windows prefer to end on a
blank line so that functions and paragraphs are less often cut in half.
"""

from __future__ import annotations

from collections.abc import Iterator

from repo_analyzer.domain import Chunk, SourceFile

# Hard ceiling so one minified or data-heavy line cannot produce a giant chunk.
MAX_LINE_CHARS = 1_000


def chunk_file(file: SourceFile, *, max_lines: int, overlap_lines: int) -> Iterator[Chunk]:
    # A window is never shorter than 3/4 of max_lines unless it ends the file, so
    # capping the overlap at half guarantees every iteration makes progress.
    if overlap_lines * 2 > max_lines:
        raise ValueError("overlap_lines must be at most half of max_lines")

    lines = [line[:MAX_LINE_CHARS] for line in file.text.splitlines()]
    total = len(lines)
    start = 0
    while start < total:
        end = _window_end(lines, start, max_lines)
        text = "\n".join(lines[start:end])
        if text.strip():
            yield Chunk(
                repo=file.repo,
                path=file.path,
                language=file.language,
                start_line=start + 1,
                end_line=end,
                text=text,
            )
        if end >= total:
            break
        start = end - overlap_lines


def _window_end(lines: list[str], start: int, max_lines: int) -> int:
    """Exclusive end index for the window beginning at ``start``."""
    hard_end = min(start + max_lines, len(lines))
    if hard_end == len(lines):
        return hard_end
    # Look for a blank line in the last quarter of the window and stop just after it.
    earliest = start + (max_lines * 3) // 4
    for index in range(hard_end - 1, earliest - 1, -1):
        if not lines[index].strip():
            return index + 1
    return hard_end
