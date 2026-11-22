"""Optional chunk-and-embed retrieval over indexed repositories.

Used by the benchmark's "JSON index + embeddings" approach: files are split into
overlapping line windows, each window is embedded, and a question retrieves the
most similar chunks by cosine similarity. Everything lives in memory (and can be
saved to one JSON file); there is no vector database.

The real embedder is `fastembed` (a local ONNX model, no API key), installed with
the ``embeddings`` extra: ``pip install -e ".[embeddings]"``. Anything with an
``embed(texts) -> list[list[float]]`` method can stand in for it, which is how
the tests run without the model.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from langchain_core.tools import BaseTool, tool

from repo_analyzer.domain import SourceFile
from repo_analyzer.errors import AnalyzerError

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
CHUNK_LINES = 60
CHUNK_OVERLAP = 15
EMBED_BATCH = 64


class EmbeddingsUnavailableError(AnalyzerError):
    """The optional embedding dependency is not installed."""


class Embedder(Protocol):
    name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class FastEmbedEmbedder:
    """Local sentence embeddings via `fastembed` (downloads the model on first use)."""

    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL) -> None:
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise EmbeddingsUnavailableError(
                'Embeddings need the optional extra: pip install -e ".[embeddings]"'
            ) from exc
        self.name = model_name
        self._model = TextEmbedding(model_name=model_name)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(x) for x in vector] for vector in self._model.embed(list(texts))]


@dataclass(frozen=True, slots=True)
class Chunk:
    repo: str
    path: str
    start_line: int
    end_line: int
    text: str

    def render(self) -> str:
        return f"{self.repo}/{self.path}:{self.start_line}-{self.end_line}\n{self.text}"


def chunk_files(
    files: Iterable[SourceFile], *, lines: int = CHUNK_LINES, overlap: int = CHUNK_OVERLAP
) -> list[Chunk]:
    """Split files into windows of ``lines`` lines that overlap by ``overlap`` lines."""
    if lines <= overlap:
        raise ValueError("lines must be greater than overlap")
    chunks: list[Chunk] = []
    step = lines - overlap
    for file in files:
        all_lines = file.text.splitlines()
        start = 0
        while start < len(all_lines):
            window = all_lines[start : start + lines]
            if "".join(window).strip():
                chunks.append(
                    Chunk(file.repo, file.path, start + 1, start + len(window), "\n".join(window))
                )
            if start + lines >= len(all_lines):
                break
            start += step
    return chunks


def _normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class EmbeddingIndex:
    """Chunks and their unit-length vectors, searched by cosine similarity."""

    def __init__(self, embedder: Embedder, chunks: list[Chunk], vectors: list[list[float]]):
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        self.embedder = embedder
        self.chunks = chunks
        self.vectors = [_normalize(v) for v in vectors]

    @classmethod
    def build(cls, embedder: Embedder, chunks: list[Chunk]) -> EmbeddingIndex:
        vectors: list[list[float]] = []
        for start in range(0, len(chunks), EMBED_BATCH):
            batch = chunks[start : start + EMBED_BATCH]
            # The path helps retrieval: "orders.py" is a strong signal for "orders".
            vectors += embedder.embed([f"{c.repo}/{c.path}\n{c.text}" for c in batch])
        return cls(embedder, chunks, vectors)

    def search(self, query: str, k: int = 8, repo: str | None = None) -> list[tuple[float, Chunk]]:
        if not self.chunks or k <= 0:
            return []
        q = _normalize(self.embedder.embed([query])[0])
        scored = [
            (sum(a * b for a, b in zip(q, vector, strict=True)), chunk)
            for chunk, vector in zip(self.chunks, self.vectors, strict=True)
            if repo is None or chunk.repo == repo
        ]
        scored.sort(key=lambda item: (-item[0], item[1].repo, item[1].path, item[1].start_line))
        return scored[:k]

    def save(self, path: Path) -> None:
        data: dict[str, Any] = {
            "embedder": self.embedder.name,
            "chunks": [asdict(c) for c in self.chunks],
            "vectors": self.vectors,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")

    @classmethod
    def load(cls, path: Path, embedder: Embedder) -> EmbeddingIndex:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("embedder") != embedder.name:
            raise ValueError(
                f"{path} was built with {data.get('embedder')!r}, not {embedder.name!r}"
            )
        return cls(embedder, [Chunk(**c) for c in data["chunks"]], data["vectors"])


def format_retrieved(results: list[tuple[float, Chunk]]) -> str:
    """Render retrieved chunks with line numbers so answers can cite them."""
    blocks = []
    for score, chunk in results:
        numbered = "\n".join(
            f"{n:>5}  {line}"
            for n, line in enumerate(chunk.text.splitlines(), start=chunk.start_line)
        )
        blocks.append(
            f"--- {chunk.repo}/{chunk.path}:{chunk.start_line}-{chunk.end_line} "
            f"(similarity {score:.2f}) ---\n{numbered}"
        )
    return "\n\n".join(blocks)


def build_semantic_search_tool(index: EmbeddingIndex, *, default_k: int = 8) -> BaseTool:
    """``semantic_search``: the most similar code chunks for a natural-language query."""

    @tool(response_format="content_and_artifact")
    def semantic_search(
        query: str, repo: str | None = None, k: int = default_k
    ) -> tuple[str, list[dict[str, Any]]]:
        """Find the code chunks most similar in meaning to a query (embeddings search).

        Returns up to `k` chunks (about 60 lines each) with their file and line numbers.

        Args:
            query: What you are looking for, in plain words or code terms.
            repo: Restrict to one repository name.
            k: Number of chunks to return (1-15).
        """
        results = index.search(query, k=max(1, min(k, 15)), repo=repo)
        if not results:
            return "No chunks found.", []
        sources = [
            {"repo": c.repo, "path": c.path, "start_line": c.start_line, "end_line": c.end_line}
            for _, c in results
        ]
        return format_retrieved(results), sources

    return semantic_search
