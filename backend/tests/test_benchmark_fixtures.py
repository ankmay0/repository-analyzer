"""The benchmark questions are well formed and the fixture apps index cleanly."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.workspace import Workspace

BENCHMARKS = Path(__file__).resolve().parents[2] / "benchmarks"
FIXTURES = BENCHMARKS / "fixtures"
CATEGORIES = {
    "data-source",
    "caching",
    "schema",
    "flow",
    "transformation",
    "external",
    "frontend",
    "config",
    "trace",
}


@pytest.fixture(scope="module")
def questions() -> dict[str, Any]:
    data = yaml.safe_load((BENCHMARKS / "questions.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_questions_file_is_well_formed(questions: dict[str, Any]) -> None:
    assert questions["version"] == 1
    assert questions["repositories"] == ["shop-api", "shop-web"]
    items = questions["questions"]
    assert 15 <= len(items) <= 25
    ids = [q["id"] for q in items]
    assert len(ids) == len(set(ids)), "question ids must be unique"
    for q in items:
        assert set(q) == {"id", "category", "question", "expected_answer", "key_facts", "evidence"}
        assert q["category"] in CATEGORIES, q["id"]
        assert len(q["question"].strip()) > 15, q["id"]
        assert len(q["expected_answer"]) > 40, q["id"]
        assert len(q["key_facts"]) >= 3, q["id"]
        assert all(isinstance(f, str) and f.strip() for f in q["key_facts"]), q["id"]


def test_every_key_fact_appears_in_its_reference_answer(questions: dict[str, Any]) -> None:
    """The reference answer must itself pass grading, or the grader is miscalibrated."""
    for q in questions["questions"]:
        answer = q["expected_answer"].lower()
        for fact in q["key_facts"]:
            alternatives = [a.strip().lower() for a in fact.split("|")]
            assert any(a in answer for a in alternatives), f"{q['id']}: {fact!r}"


def test_every_evidence_file_exists(questions: dict[str, Any]) -> None:
    for q in questions["questions"]:
        assert q["evidence"], q["id"]
        for ref in q["evidence"]:
            assert (FIXTURES / ref).is_file(), f"{q['id']}: missing {ref}"


def test_fixture_python_parses() -> None:
    for path in (FIXTURES / "shop-api").rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_fixtures_index_with_endpoints_symbols_and_dependencies(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", allow_local_paths=True)
    store = RepoIndexStore(settings.repos_dir)
    fetcher = RepoFetcher(
        settings.checkouts_dir, allowed_hosts=settings.allowed_git_hosts, allow_local_paths=True
    )
    service = IndexingService(settings, fetcher, store, Workspace(settings.workspace_file))

    state = service.run([str(FIXTURES / "shop-api"), str(FIXTURES / "shop-web")])

    assert [r.name for r in state.repositories] == ["shop-api", "shop-web"]
    api = store.load(state.repositories[0].index_file)
    web = store.load(state.repositories[1].index_file)

    api_functions = {
        f["name"] for file in api["files"] for f in file.get("symbols", {}).get("functions", [])
    }
    assert {"create_order", "fulfill_order", "price_order", "book_shipment"} <= api_functions
    assert {d["name"] for d in api["dependencies"]} >= {"fastapi", "psycopg", "httpx"}
    assert {d["name"] for d in web["dependencies"]} >= {"react", "@tanstack/react-query"}
    web_paths = {f["path"] for f in web["files"]}
    assert "src/components/OrderForm.tsx" in web_paths
