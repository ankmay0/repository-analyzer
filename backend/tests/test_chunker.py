from __future__ import annotations

import itertools

import pytest

from repo_analyzer.domain import SourceFile
from repo_analyzer.ingestion.chunker import MAX_LINE_CHARS, chunk_file


def make_file(lines: list[str]) -> SourceFile:
    return SourceFile(repo="r", path="a.py", language="python", text="\n".join(lines))


def test_small_file_is_one_chunk_with_exact_line_range() -> None:
    chunks = list(chunk_file(make_file(["a", "b", "c"]), max_lines=10, overlap_lines=2))

    assert len(chunks) == 1
    assert (chunks[0].start_line, chunks[0].end_line) == (1, 3)
    assert chunks[0].text == "a\nb\nc"
    assert chunks[0].citation == "r/a.py:1-3"
    assert chunks[0].id == "r:a.py:1-3"


@pytest.mark.parametrize("total", [1, 9, 10, 11, 25, 100, 257])
def test_every_line_is_covered_and_ranges_match_the_text(total: int) -> None:
    lines = [f"line {n}" for n in range(1, total + 1)]
    chunks = list(chunk_file(make_file(lines), max_lines=10, overlap_lines=3))

    covered: set[int] = set()
    for chunk in chunks:
        assert chunk.end_line - chunk.start_line + 1 <= 10
        assert chunk.text.split("\n") == lines[chunk.start_line - 1 : chunk.end_line]
        covered.update(range(chunk.start_line, chunk.end_line + 1))
    assert covered == set(range(1, total + 1))
    assert len({chunk.id for chunk in chunks}) == len(chunks)


def test_consecutive_chunks_overlap() -> None:
    lines = [f"line {n}" for n in range(1, 31)]
    chunks = list(chunk_file(make_file(lines), max_lines=10, overlap_lines=3))

    for previous, current in itertools.pairwise(chunks):
        assert current.start_line == previous.end_line - 3 + 1


def test_prefers_to_break_on_a_blank_line() -> None:
    lines = [f"line {n}" for n in range(1, 21)]
    lines[8] = ""  # line 9 is blank and falls in the last quarter of a 10-line window
    chunks = list(chunk_file(make_file(lines), max_lines=10, overlap_lines=0))

    assert chunks[0].end_line == 9


def test_very_long_lines_are_truncated() -> None:
    chunks = list(chunk_file(make_file(["x" * 5_000]), max_lines=10, overlap_lines=0))
    assert len(chunks[0].text) == MAX_LINE_CHARS


def test_rejects_overlap_that_could_stall_progress() -> None:
    with pytest.raises(ValueError, match="overlap_lines"):
        list(chunk_file(make_file(["a"]), max_lines=10, overlap_lines=6))
