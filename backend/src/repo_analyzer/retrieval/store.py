"""Persistent vector index over code chunks (Chroma, cosine similarity).

Embeddings are computed by the injected ``Embedder`` rather than by Chroma, so
the store never downloads a model of its own and the embedding backend can be
swapped without touching this module. The embedder's name is recorded on the
collection; opening an index with a different embedder is an error instead of
a silent source of meaningless search results.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from repo_analyzer.domain import Chunk
from repo_analyzer.errors import IndexIncompatibleError
from repo_analyzer.retrieval.embeddings import Embedder

logger = logging.getLogger(__name__)

_COLLECTION = "code_chunks"
_EMBEDDER_KEY = "embedder"
_EMBED_BATCH = 64


@dataclass(frozen=True, slots=True)
class SearchHit:
    chunk: Chunk
    score: float  # cosine similarity in [-1, 1]; higher is closer


class VectorStore:
    def __init__(self, path: Path, embedder: Embedder) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._embedder = embedder
        self._client = chromadb.PersistentClient(
            path=str(path), settings=ChromaSettings(anonymized_telemetry=False)
        )
        self._collection = self._client.get_or_create_collection(
            name=_COLLECTION,
            metadata={_EMBEDDER_KEY: embedder.name},
            configuration={"hnsw": {"space": "cosine"}},
            embedding_function=None,
        )
        built_with = (self._collection.metadata or {}).get(_EMBEDDER_KEY)
        if built_with != embedder.name:
            raise IndexIncompatibleError(
                f"The index at {path} was built with embedder {built_with!r} but "
                f"{embedder.name!r} is configured. Delete the index directory and re-index."
            )

    def count(self) -> int:
        return int(self._collection.count())

    def delete_repo(self, repo: str) -> None:
        self._collection.delete(where={"repo": repo})

    def replace_repo(self, repo: str, chunks: Sequence[Chunk]) -> None:
        """Replace everything indexed for ``repo`` with ``chunks``."""
        self.delete_repo(repo)
        for batch in _batched(chunks, _EMBED_BATCH):
            self._collection.add(
                ids=[chunk.id for chunk in batch],
                # The path is embedded with the text: "where is X configured" style
                # questions are often answered by the file name as much as its body.
                embeddings=self._embedder.embed_documents(
                    [f"{chunk.repo}/{chunk.path}\n{chunk.text}" for chunk in batch]
                ),  # type: ignore[arg-type]
                documents=[chunk.text for chunk in batch],
                metadatas=[
                    {
                        "repo": chunk.repo,
                        "path": chunk.path,
                        "language": chunk.language,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                    }
                    for chunk in batch
                ],
            )
        logger.info("Indexed %d chunks for %s", len(chunks), repo)

    def search(self, query: str, *, top_k: int, repo: str | None = None) -> list[SearchHit]:
        if self.count() == 0:
            return []
        result: Any = self._collection.query(
            query_embeddings=[self._embedder.embed_query(query)],  # type: ignore[arg-type]
            n_results=top_k,
            where={"repo": repo} if repo else None,
            include=["documents", "metadatas", "distances"],
        )
        hits = []
        for document, metadata, distance in zip(
            result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        ):
            chunk = Chunk(
                repo=str(metadata["repo"]),
                path=str(metadata["path"]),
                language=str(metadata["language"]),
                start_line=int(metadata["start_line"]),
                end_line=int(metadata["end_line"]),
                text=document,
            )
            hits.append(SearchHit(chunk=chunk, score=1.0 - float(distance)))
        return hits


def _batched(items: Sequence[Chunk], size: int) -> Iterator[Sequence[Chunk]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
