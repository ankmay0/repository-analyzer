from __future__ import annotations

import importlib.util
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from repo_analyzer.domain import SourceFile
from repo_analyzer.retrieval.embeddings import (
    EmbeddingIndex,
    EmbeddingsUnavailableError,
    FastEmbedEmbedder,
    build_semantic_search_tool,
    chunk_files,
)

VOCABULARY = ["order", "price", "tax", "cache", "customer", "carrier", "shipping"]


class KeywordEmbedder:
    """Counts vocabulary words: crude, deterministic, and enough to test ranking."""

    name = "keyword-test"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [
            [float(len(re.findall(word, text.lower()))) + 0.01 for word in VOCABULARY]
            for text in texts
        ]


def files() -> list[SourceFile]:
    return [
        SourceFile("shop", "pricing.py", "python", "tax tax price\nprice\n"),
        SourceFile("shop", "cache.py", "python", "cache cache\n" * 3),
        SourceFile("web", "carrier.ts", "typescript", "carrier shipping\n"),
    ]


def test_chunks_are_overlapping_windows_with_line_numbers() -> None:
    text = "\n".join(f"line {n}" for n in range(1, 101))
    chunks = chunk_files([SourceFile("r", "a.py", "python", text)], lines=40, overlap=10)
    assert [(c.start_line, c.end_line) for c in chunks] == [(1, 40), (31, 70), (61, 100)]
    assert chunk_files([SourceFile("r", "blank.py", "python", "\n\n")]) == []
    with pytest.raises(ValueError):
        chunk_files([], lines=5, overlap=5)


def test_search_ranks_by_similarity_and_filters_by_repo(tmp_path: Path) -> None:
    index = EmbeddingIndex.build(KeywordEmbedder(), chunk_files(files()))
    [(score, best), *_] = index.search("what is the tax on a price", k=3)
    assert best.path == "pricing.py" and score > 0.5
    assert [c.repo for _, c in index.search("shipping", repo="web")] == ["web"]

    index.save(tmp_path / "e.json")
    again = EmbeddingIndex.load(tmp_path / "e.json", KeywordEmbedder())
    assert [c.path for _, c in again.search("cache", k=1)] == ["cache.py"]


def test_semantic_search_tool_returns_numbered_chunks_and_sources() -> None:
    index = EmbeddingIndex.build(KeywordEmbedder(), chunk_files(files()))
    tool = build_semantic_search_tool(index, default_k=2)
    message = tool.invoke(
        {"type": "tool_call", "id": "1", "name": tool.name, "args": {"query": "cache", "k": 1}}
    )
    assert str(message.content).startswith("--- shop/cache.py:1-3 (similarity")
    assert "    1  cache cache" in str(message.content)
    assert message.artifact == [
        {"repo": "shop", "path": "cache.py", "start_line": 1, "end_line": 3}
    ]


@pytest.mark.skipif(importlib.util.find_spec("fastembed") is not None, reason="fastembed installed")
def test_fastembed_missing_gives_install_hint() -> None:
    with pytest.raises(EmbeddingsUnavailableError, match=r"\.\[embeddings\]"):
        FastEmbedEmbedder()
