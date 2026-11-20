from __future__ import annotations

import math
from pathlib import Path

import pytest

from repo_analyzer.domain import Chunk
from repo_analyzer.errors import IndexIncompatibleError
from repo_analyzer.retrieval.embeddings import HashingEmbedder, tokenize_identifiers
from repo_analyzer.retrieval.store import VectorStore


def chunk(repo: str, path: str, text: str) -> Chunk:
    return Chunk(repo=repo, path=path, language="python", start_line=1, end_line=3, text=text)


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_tokenizer_splits_identifiers() -> None:
    assert tokenize_identifiers("parseHTTPResponse(user_id2, MAX_SIZE)") == [
        "parse", "httpresponse", "user", "id2", "max", "size",
    ]  # fmt: skip


def test_hashing_embeddings_are_unit_length_and_reflect_shared_vocabulary() -> None:
    embedder = HashingEmbedder()
    query = embedder.embed_query("create a login session")
    related, unrelated = embedder.embed_documents(
        ["def create_session(user): login", "SELECT price FROM invoices"]
    )

    assert math.isclose(cosine(query, query), 1.0, rel_tol=1e-9)
    assert cosine(query, related) > cosine(query, unrelated)
    assert embedder.embed_query("") == [0.0] * 512


def test_search_returns_the_closest_chunk_first(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "index", HashingEmbedder())
    store.replace_repo(
        "a",
        [
            chunk("a", "auth.py", "def verify_password(password, hash): ..."),
            chunk("a", "billing.py", "def charge_invoice(invoice, amount): ..."),
        ],
    )

    hits = store.search("verify the user's password", top_k=2)

    assert [h.chunk.path for h in hits] == ["auth.py", "billing.py"]
    assert hits[0].score > hits[1].score
    assert hits[0].chunk.citation == "a/auth.py:1-3"


def test_search_can_be_restricted_to_one_repository(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "index", HashingEmbedder())
    store.replace_repo("a", [chunk("a", "x.py", "shared words here")])
    store.replace_repo("b", [chunk("b", "y.py", "shared words here")])

    assert {h.chunk.repo for h in store.search("shared words", top_k=5)} == {"a", "b"}
    assert {h.chunk.repo for h in store.search("shared words", top_k=5, repo="b")} == {"b"}


def test_replace_repo_removes_previous_chunks_for_that_repo_only(tmp_path: Path) -> None:
    store = VectorStore(tmp_path / "index", HashingEmbedder())
    store.replace_repo("a", [chunk("a", "old.py", "old"), chunk("a", "older.py", "older")])
    store.replace_repo("b", [chunk("b", "keep.py", "keep")])

    store.replace_repo("a", [chunk("a", "new.py", "new")])

    assert store.count() == 2
    assert {h.chunk.path for h in store.search("anything", top_k=10)} == {"new.py", "keep.py"}


def test_empty_index_returns_no_results(tmp_path: Path) -> None:
    assert VectorStore(tmp_path / "index", HashingEmbedder()).search("x", top_k=3) == []


def test_index_survives_reopening_and_rejects_a_different_embedder(tmp_path: Path) -> None:
    VectorStore(tmp_path / "index", HashingEmbedder()).replace_repo(
        "a", [chunk("a", "x.py", "persisted text")]
    )

    assert VectorStore(tmp_path / "index", HashingEmbedder()).count() == 1
    with pytest.raises(IndexIncompatibleError, match="re-index"):
        VectorStore(tmp_path / "index", HashingEmbedder(dimension=64))
