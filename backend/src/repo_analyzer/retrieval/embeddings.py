"""Text embedding backends.

The rest of the system depends only on the ``Embedder`` protocol. Two
implementations are provided:

* ``FastEmbedEmbedder`` - a local ONNX sentence-embedding model. No API key and
  no data leaves the machine; the model is downloaded on first use.
* ``HashingEmbedder`` - a dependency-free lexical embedding (signed feature
  hashing over identifier sub-tokens). It needs no model download, which makes
  it the right choice for tests and air-gapped environments, but it only
  matches shared vocabulary, not meaning.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Any, Protocol

from repo_analyzer.config import Settings


class Embedder(Protocol):
    @property
    def name(self) -> str:
        """Stable identifier; an index built with one name is unusable with another."""
        ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model: Any = None

    @property
    def name(self) -> str:
        return f"fastembed:{self._model_name}"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._load().embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        # query_embed applies the model's query instruction prefix where it has one.
        vector = next(iter(self._load().query_embed(text)))
        return list(vector.tolist())

    def _load(self) -> Any:
        if self._model is None:
            # Imported lazily: importing fastembed pulls in onnxruntime, which is slow
            # and unnecessary when another backend is configured.
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self._model_name)
        return self._model


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize_identifiers(text: str) -> list[str]:
    """Lower-cased word tokens, with camelCase and snake_case identifiers split."""
    tokens: list[str] = []
    for word in _WORD.findall(text):
        tokens.extend(part.lower() for part in _CAMEL_BOUNDARY.split(word) if part)
    return tokens


class HashingEmbedder:
    def __init__(self, dimension: int = 512) -> None:
        self._dimension = dimension

    @property
    def name(self) -> str:
        return f"hashing:{self._dimension}"

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vector = [0.0] * self._dimension
        for token in tokenize_identifiers(text):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self._dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        # Dampen term frequency, then L2-normalise so cosine distance is meaningful.
        vector = [math.copysign(math.log1p(abs(v)), v) for v in vector]
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else vector


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedding_provider == "hashing":
        return HashingEmbedder()
    return FastEmbedEmbedder(settings.embedding_model)
