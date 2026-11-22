"""The benchmark HTTP API, with a scripted model, a mock GitHub API and a keyword embedder."""

from __future__ import annotations

import csv
import io
import json
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, BaseMessage

from repo_analyzer.api.app import create_app
from repo_analyzer.benchmark import service as benchmark_service
from repo_analyzer.benchmark.approaches import APPROACHES
from repo_analyzer.config import Settings
from support import FunctionChatModel, github_fixture_transport
from test_benchmark import BENCHMARKS, QUESTIONS, fake_model
from test_embeddings import KeywordEmbedder

FIRST_TWO = [q.id for q in QUESTIONS.questions[:2]]


def make_settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        benchmarks_dir=BENCHMARKS,
        **extra,
    )


def make_app(settings: Settings, respond: Callable[[list[BaseMessage]], AIMessage]) -> FastAPI:
    model = FunctionChatModel(respond=respond)
    transport, _ = github_fixture_transport(
        BENCHMARKS / "fixtures", owner="o", repo="r", ref="main", prefix="benchmarks/fixtures"
    )
    return create_app(
        settings,
        model_factory=lambda: model,
        benchmark_github_transport=transport,
        benchmark_embedder_factory=KeywordEmbedder,
    )


async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    settings = make_settings(
        tmp_path, GITHUB_TOKEN="t", benchmark_github_repo="o/r", benchmark_github_ref="main"
    )
    async for http in client_for(make_app(settings, fake_model)):
        yield http


@pytest.fixture
async def bare_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A server with no API key, no GITHUB_TOKEN and no fastembed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(benchmark_service, "fastembed_available", lambda: False)
    app = create_app(make_settings(tmp_path))
    async for http in client_for(app):
        yield http


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


async def finished(client: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    """Follow the event stream to its end, then read the final snapshot."""
    stream = await client.get(f"/api/benchmark/runs/{run_id}/events")
    assert stream.status_code == 200
    assert parse_sse(stream.text)[-1][0] in ("finished", "failed")
    return (await client.get(f"/api/benchmark/runs/{run_id}")).json()


async def test_questions_endpoint_lists_the_known_answer_questions(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/benchmark/questions")).json()

    assert body["repositories"] == ["shop-api", "shop-web"]
    assert [q["id"] for q in body["questions"]] == [q.id for q in QUESTIONS.questions]
    first = body["questions"][0]
    assert first["key_facts"] and first["expected_answer"] and first["category"]


async def test_status_reports_every_approach_as_available_when_configured(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/benchmark/status")).json()

    assert body["llm_configured"] is True
    assert [a["name"] for a in body["approaches"]] == list(APPROACHES)
    assert all(a["available"] and a["reason"] is None for a in body["approaches"])
    assert body["repositories"] == ["shop-api", "shop-web"]
    assert body["running_run_id"] is None


async def test_status_gives_a_reason_for_each_approach_that_cannot_run(bare_client: Any) -> None:
    body = (await bare_client.get("/api/benchmark/status")).json()

    assert body["llm_configured"] is False
    assert "ANTHROPIC_API_KEY" in body["llm_message"]
    by_name = {a["name"]: a for a in body["approaches"]}
    assert by_name["json-github"]["available"] is False
    assert "GITHUB_TOKEN" in by_name["json-github"]["reason"]
    assert by_name["json-embeddings"]["available"] is False
    assert "fastembed" in by_name["json-embeddings"]["reason"]
    assert by_name["json-metadata"]["available"] and by_name["tools-only"]["available"]


async def test_starting_without_an_api_key_is_a_clear_error(bare_client: Any) -> None:
    response = await bare_client.post("/api/benchmark/runs", json={"approaches": ["tools-only"]})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


async def test_unavailable_approach_is_rejected_with_its_reason(tmp_path: Path) -> None:
    app = make_app(make_settings(tmp_path), fake_model)  # no GITHUB_TOKEN
    async for http in client_for(app):
        response = await http.post("/api/benchmark/runs", json={"approaches": ["json-github"]})

    assert response.status_code == 400
    assert "json-github is unavailable" in response.json()["detail"]
    assert "GITHUB_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"approaches": []}, 422, "at least 1"),
        ({"approaches": ["magic"]}, 400, "Unknown approach"),
        ({"approaches": ["tools-only"], "question_ids": ["nope"]}, 400, "Unknown question"),
        ({"approaches": ["tools-only"], "repositories": ["x"]}, 400, "Unknown fixture"),
    ],
)
async def test_invalid_requests_are_rejected(
    client: httpx.AsyncClient, body: dict[str, Any], status: int, text: str
) -> None:
    response = await client.post("/api/benchmark/runs", json=body)

    assert response.status_code == status
    assert text in json.dumps(response.json()["detail"])


async def test_unknown_run_is_a_404(client: httpx.AsyncClient) -> None:
    for path in ("", "/events", "/results.csv"):
        assert (await client.get(f"/api/benchmark/runs/nope{path}")).status_code == 404


async def test_a_run_streams_progress_and_reports_results(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    started = await client.post(
        "/api/benchmark/runs",
        json={"approaches": list(APPROACHES), "question_ids": FIRST_TWO, "judge": True},
    )
    assert started.status_code == 202
    run_id = started.json()["id"]
    assert started.json()["total"] == 8

    stream = await client.get(f"/api/benchmark/runs/{run_id}/events")
    events = parse_sse(stream.text)
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "plan" and kinds[-1] == "finished"
    assert kinds.count("result") == 8
    assert kinds.count("question_started") == 8
    assert kinds.count("approach_started") == 4
    assert kinds.count("approach_setup") == 2  # metadata and embeddings
    progress = [data["progress"]["completed"] for kind, data in events if kind == "result"]
    assert progress == list(range(1, 9))
    first_result = next(data for kind, data in events if kind == "result")
    assert first_result["result"]["approach"] == "json-github"
    assert first_result["result"]["matched_facts"]

    # Resuming a stream skips what the client already has.
    resumed = parse_sse((await client.get(f"/api/benchmark/runs/{run_id}/events?after=10")).text)
    assert [kind for kind, _ in resumed] == kinds[10:]

    run = (await client.get(f"/api/benchmark/runs/{run_id}")).json()
    assert run["state"] == "finished" and run["error"] is None
    assert (run["completed"], run["total"], run["current"]) == (8, 8, None)
    assert len(run["rows"]) == 8
    assert all(row["correct"] and row["judge_correct"] for row in run["rows"])
    assert {s["approach"] for s in run["setup"]} == {"json-metadata", "json-embeddings"}
    assert [row["approach"] for row in run["summary"]] == list(APPROACHES)
    assert run["summary"][0]["correct"] == 2
    assert run["next_event"] == len(events)

    # The same report files the command line tool writes.
    out = tmp_path / "data" / "benchmark" / run_id
    assert (out / "summary.md").is_file() and (out / "results.csv").is_file()


async def test_csv_download_has_one_row_per_answer(client: httpx.AsyncClient) -> None:
    started = await client.post(
        "/api/benchmark/runs", json={"approaches": ["tools-only"], "question_ids": FIRST_TWO}
    )
    run_id = started.json()["id"]
    await finished(client, run_id)

    response = await client.get(f"/api/benchmark/runs/{run_id}/results.csv")

    assert response.headers["content-type"].startswith("text/csv")
    assert f"benchmark-{run_id}.csv" in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert [r["question_id"] for r in rows] == FIRST_TWO
    assert rows[0]["approach"] == "tools-only" and rows[0]["total_tokens"] == "2470"


async def test_wrong_answers_are_graded_wrong(tmp_path: Path) -> None:
    app = make_app(make_settings(tmp_path), lambda _: AIMessage(content="I do not know."))
    async for http in client_for(app):
        started = await http.post(
            "/api/benchmark/runs", json={"approaches": ["tools-only"], "question_ids": FIRST_TWO}
        )
        run = await finished(http, started.json()["id"])

    assert [row["correct"] for row in run["rows"]] == [False, False]
    assert run["rows"][0]["matched_facts"] == []
    assert run["rows"][0]["missing_facts"]
    assert run["summary"][0]["correct"] == 0


async def test_only_one_run_at_a_time(tmp_path: Path) -> None:
    release = threading.Event()

    def blocked(messages: list[BaseMessage]) -> AIMessage:
        assert release.wait(timeout=30)
        return fake_model(messages)

    app = make_app(make_settings(tmp_path), blocked)
    async for http in client_for(app):
        body = {"approaches": ["tools-only"], "question_ids": FIRST_TWO[:1]}
        first = await http.post("/api/benchmark/runs", json=body)
        assert first.status_code == 202

        again = await http.post("/api/benchmark/runs", json=body)
        assert again.status_code == 409
        assert first.json()["id"] in again.json()["detail"]
        assert (await http.get("/api/benchmark/status")).json()["running_run_id"] == (
            first.json()["id"]
        )
        assert (await http.get(f"/api/benchmark/runs/{first.json()['id']}")).json()[
            "state"
        ] == "running"

        release.set()
        run = await finished(http, first.json()["id"])
        assert run["state"] == "finished"
        second = await http.post("/api/benchmark/runs", json=body)
        assert second.status_code == 202  # a finished run no longer blocks
        await finished(http, second.json()["id"])


async def test_a_crashing_run_ends_as_failed_with_the_reason(tmp_path: Path) -> None:
    def broken() -> Any:
        raise RuntimeError("boom")

    app = create_app(make_settings(tmp_path), model_factory=broken)
    async for http in client_for(app):
        started = await http.post("/api/benchmark/runs", json={"approaches": ["json-metadata"]})
        run = await finished(http, started.json()["id"])

    assert run["state"] == "failed"
    assert run["error"] == "RuntimeError: boom"
    assert run["rows"] == []


async def test_a_run_covers_only_the_chosen_approaches_and_questions(tmp_path: Path) -> None:
    app = make_app(make_settings(tmp_path), fake_model)
    async for http in client_for(app):
        started = await http.post(
            "/api/benchmark/runs",
            json={"approaches": ["tools-only", "json-embeddings"], "question_ids": FIRST_TWO},
        )
        run = await finished(http, started.json()["id"])

    assert run["state"] == "finished" and run["skipped"] == {}
    assert (run["total"], run["completed"]) == (4, 4)
    assert [(r["approach"], r["question_id"]) for r in run["rows"]] == [
        (a, q) for a in ("tools-only", "json-embeddings") for q in FIRST_TWO
    ]
