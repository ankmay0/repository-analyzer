"""POST /api/compare: one question under every approach at once, with a scripted model."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage, BaseMessage, SystemMessage, ToolMessage

from repo_analyzer.api.app import create_app
from repo_analyzer.benchmark import service as benchmark_service
from repo_analyzer.benchmark.approaches import APPROACHES
from repo_analyzer.benchmark.compare import FIXTURES_ID
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.graph_extract import GraphRecords
from repo_analyzer.ingestion.graph_store import graph_workspace_key
from support import FakeGraphStore, FunctionChatModel, tool_call, usage
from test_benchmark import BENCHMARKS
from test_embeddings import KeywordEmbedder


def make_settings(tmp_path: Path, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        benchmarks_dir=BENCHMARKS,
        **extra,
    )


async def client_for(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        if "event" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    return events


QUESTION = "How does the shop decide what a customer pays?"
# Distinct totals per approach so each column's usage can be told apart.
INPUT_TOKENS = {
    "chat": 4000,
    "json-metadata": 1500,
    "tools-only": 9000,
    "json-embeddings": 2500,
    "json-graph": 2000,
}
MARKERS = {  # first match wins: json-graph's prompt also says it cannot read the source
    "A code graph built from the code": "json-graph",
    "indexed for you": "chat",
    "You cannot read the source code": "json-metadata",
    "There is no index": "tools-only",
    "You cannot read files directly": "json-embeddings",
}


def approach_of(system: str) -> str:
    return next(name for marker, name in MARKERS.items() if marker in system)


class Script:
    """Scripted model: one tool call, then an answer whose size depends on the approach."""

    def __init__(self, *, fail: str | None = None, rendezvous: int | None = None) -> None:
        self.fail = fail
        self.barrier = threading.Barrier(rendezvous) if rendezvous else None
        self.tool_outputs: list[str] = []
        self.metadata_calls = 0

    def __call__(self, messages: list[BaseMessage]) -> AIMessage:
        system = str(messages[0].content) if isinstance(messages[0], SystemMessage) else ""
        if system.startswith("You document how data flows"):
            self.metadata_calls += 1
            entry = {"kind": "endpoint", "name": "POST /orders", "location": "app/orders.py:3"}
            return AIMessage(
                content=json.dumps({"entries": [entry]}),
                usage_metadata=usage(5000, 300),
            )
        approach = approach_of(system)
        if not isinstance(messages[-1], ToolMessage):
            if self.barrier is not None:
                self.barrier.wait(timeout=20)  # only passes if approaches run concurrently
            return tool_call("list_repositories", {}, usage=usage(1000, 50))
        self.tool_outputs.append(str(messages[-1].content))
        if approach == self.fail:
            raise RuntimeError("model exploded")
        return AIMessage(
            content=f"Answer from {approach}.",
            usage_metadata=usage(INPUT_TOKENS[approach], 100, cache_read=500),
        )


def make_app(
    settings: Settings,
    script: Script,
    *,
    embedder: bool = True,
    graph: FakeGraphStore | None = None,
) -> FastAPI:
    model = FunctionChatModel(respond=script)
    return create_app(
        settings.model_copy(update={"graph_enabled": True}),
        model_factory=lambda: model,
        benchmark_embedder_factory=KeywordEmbedder if embedder else None,
        graph_store=graph or FakeGraphStore(),
    )


def by_approach(events: list[tuple[str, dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for _, data in events:
        if "approach" in data:
            grouped[data["approach"]].append(data)
    return grouped


async def ask(client: httpx.AsyncClient, **body: Any) -> list[tuple[str, dict[str, Any]]]:
    response = await client.post("/api/compare", json={"question": QUESTION, **body})
    assert response.status_code == 200, response.text
    return parse_sse(response.text)


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    async for http in client_for(make_app(make_settings(tmp_path), Script())):
        yield http


async def test_status_lists_the_fixtures_and_each_approach(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/compare/status")).json()

    assert body["llm_configured"] is True
    assert [r["id"] for r in body["repositories"]] == [FIXTURES_ID]
    assert [a["name"] for a in body["approaches"]] == list(APPROACHES)
    assert all(a["available"] for a in body["approaches"])
    assert body["approaches"][0]["name"] == "chat"
    assert body["approaches"][0]["label"] == "Chat agent"


async def test_status_explains_what_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(benchmark_service, "fastembed_available", lambda: False)
    async for http in client_for(create_app(make_settings(tmp_path))):
        body = (await http.get("/api/compare/status")).json()
        refused = await http.post("/api/compare", json={"question": QUESTION})

    assert "ANTHROPIC_API_KEY" in body["llm_message"]
    reasons = {a["name"]: a["reason"] for a in body["approaches"]}
    assert reasons["chat"] is None and reasons["json-metadata"] is None
    assert reasons["tools-only"] is None
    assert "fastembed" in reasons["json-embeddings"]
    assert "RA_GRAPH_ENABLED" in reasons["json-graph"]
    assert refused.status_code == 503
    assert "ANTHROPIC_API_KEY" in refused.json()["detail"]


async def test_every_approach_streams_its_answer_and_usage(client: httpx.AsyncClient) -> None:
    events = await ask(client)
    kinds = [kind for kind, _ in events]
    grouped = by_approach(events)

    assert kinds[0] == "plan" and kinds[-1] == "done"
    plan = events[0][1]
    assert plan["question"] == QUESTION and plan["repository"] == FIXTURES_ID
    assert [a["name"] for a in plan["approaches"]] == list(APPROACHES)
    assert plan["approaches"][0]["label"] == "Chat agent"
    assert all(a["available"] for a in plan["approaches"])
    assert set(grouped) == set(APPROACHES)
    for name in APPROACHES:
        mine = grouped[name]
        text = "".join(e["text"] for e in mine if e["type"] == "token")
        assert text == f"Answer from {name}."
        assert [e["type"] for e in mine][-2:] == ["usage", "approach_done"]
        usage_event = next(e for e in mine if e["type"] == "usage")
        total = usage_event["usage"]
        assert total["input_tokens"] == 1000 + INPUT_TOKENS[name]
        assert total["output_tokens"] == 150
        assert (total["cache_read_tokens"], total["llm_calls"], total["tool_calls"]) == (500, 2, 1)
        assert total["latency_ms"] >= 0
        assert [e["name"] for e in mine if e["type"] == "tool_call"] == ["list_repositories"]
    # Only the approaches that prepare something report setup cost.
    setup = {n: next(e for e in grouped[n] if e["type"] == "usage")["setup"] for n in APPROACHES}
    assert setup["json-metadata"]["total_tokens"] == 2 * 5300  # one model call per fixture app
    assert setup["json-embeddings"]["total_tokens"] == 0
    assert setup["json-graph"]["total_tokens"] == 0
    assert setup["tools-only"] is None and setup["chat"] is None


async def test_setup_is_paid_once_and_reused_for_the_next_question(tmp_path: Path) -> None:
    script = Script()
    async for http in client_for(make_app(make_settings(tmp_path), script)):
        await ask(http)
        second = by_approach(await ask(http))

    usage_event = next(e for e in second["json-metadata"] if e["type"] == "usage")
    assert usage_event["setup"] is None
    assert script.metadata_calls == 2  # the two fixture apps, once


async def test_approaches_run_concurrently(tmp_path: Path) -> None:
    # All five first model calls must be waiting at once for the barrier to open.
    async for http in client_for(make_app(make_settings(tmp_path), Script(rendezvous=5))):
        events = await ask(http)

    assert not any(data["type"] == "error" for _, data in events if "type" in data)
    assert [k for k, _ in events].count("usage") == 5


async def test_unavailable_approaches_are_reported_and_the_rest_still_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(benchmark_service, "fastembed_available", lambda: False)
    app = make_app(make_settings(tmp_path), Script(), embedder=False)
    async for http in client_for(app):
        events = await ask(http)

    plan = {a["name"]: a for a in events[0][1]["approaches"]}
    assert plan["json-embeddings"]["available"] is False
    assert "fastembed" in plan["json-embeddings"]["reason"]
    assert plan["chat"]["available"] is True
    grouped = by_approach(events)
    assert "json-embeddings" not in grouped
    assert set(grouped) == {"chat", "json-metadata", "tools-only", "json-graph"}


async def test_json_graph_loads_the_fixtures_into_their_own_graph_namespace(
    tmp_path: Path,
) -> None:
    graph = FakeGraphStore()
    settings = make_settings(tmp_path)
    async for http in client_for(make_app(settings, Script(), graph=graph)):
        grouped = by_approach(await ask(http, approaches=["json-graph"]))

    fixtures_ws = graph_workspace_key(settings.data_dir / "compare" / "fixtures")
    assert {repo for ws, repo in graph.repos if ws == fixtures_ws} == {"shop-api", "shop-web"}
    assert graph.trace_endpoint(fixtures_ws, "shop-api", "POST", "/orders") is not None
    assert grouped["json-graph"][0] == {
        "type": "setup", "approach": "json-graph", "message": "Loading the code graph",
    }  # fmt: skip


async def test_json_graph_is_unavailable_when_neo4j_is_down(tmp_path: Path) -> None:
    app = make_app(make_settings(tmp_path), Script(), graph=FakeGraphStore(reachable=False))
    async for http in client_for(app):
        events = await ask(http, approaches=["json-graph", "tools-only"])

    plan = {a["name"]: a for a in events[0][1]["approaches"]}
    assert plan["json-graph"]["available"] is False
    assert "not reachable" in plan["json-graph"]["reason"]
    assert set(by_approach(events)) == {"tools-only"}


async def test_one_failing_approach_does_not_stop_the_others(tmp_path: Path) -> None:
    app = make_app(make_settings(tmp_path), Script(fail="tools-only"))
    async for http in client_for(app):
        grouped = by_approach(await ask(http))

    errors = [e for e in grouped["tools-only"] if e["type"] == "error"]
    assert errors and "Unexpected server error" in errors[0]["message"]
    assert grouped["tools-only"][-1]["type"] == "approach_done"
    for name in ("chat", "json-metadata", "json-embeddings"):
        assert any(e["type"] == "usage" for e in grouped[name])


async def test_a_subset_of_approaches_can_be_asked_for(client: httpx.AsyncClient) -> None:
    events = await ask(client, approaches=["tools-only", "json-metadata"])

    assert [a["name"] for a in events[0][1]["approaches"]] == ["tools-only", "json-metadata"]
    assert set(by_approach(events)) == {"tools-only", "json-metadata"}


async def test_an_indexed_repository_is_compared_in_isolation(
    settings: Settings, indexed_workspace: Any, fixture_repos: dict[str, Path]
) -> None:
    names = sorted(fixture_repos)
    script = Script()
    app = make_app(settings.model_copy(update={"benchmarks_dir": BENCHMARKS}), script)
    async for http in client_for(app):
        status = (await http.get("/api/compare/status")).json()
        events = await ask(http, repository=names[0])

    assert {r["id"] for r in status["repositories"]} == {FIXTURES_ID, *names}
    plan = {a["name"]: a for a in events[0][1]["approaches"]}
    assert all(plan[n]["available"] for n in APPROACHES)
    grouped = by_approach(events)
    assert all(any(e["type"] == "usage" for e in grouped[n]) for n in grouped)
    # The agent only sees the chosen repository, not the rest of the workspace.
    listed = [out for out in script.tool_outputs if names[0] in out]
    assert listed and not any(other in out for out in listed for other in names[1:])
    assert script.metadata_calls == 1


@pytest.mark.parametrize(
    ("body", "status", "text"),
    [
        ({"repository": "nope"}, 404, "Unknown repository"),
        ({"approaches": ["magic"]}, 400, "Unknown approach"),
        ({"question": ""}, 422, "at least 1"),
    ],
)
async def test_bad_requests_are_rejected_before_streaming(
    client: httpx.AsyncClient, body: dict[str, Any], status: int, text: str
) -> None:
    response = await client.post("/api/compare", json={"question": QUESTION, **body})

    assert response.status_code == status
    assert text in str(response.json()["detail"])


async def test_an_indexed_repository_reuses_the_graph_its_indexing_run_loaded(
    settings: Settings, indexed_workspace: Any, fixture_repos: dict[str, Path]
) -> None:
    name = sorted(fixture_repos)[0]
    graph = FakeGraphStore()
    main_ws = graph_workspace_key(settings.data_dir)
    graph.sync_repo(main_ws, name, None, GraphRecords())  # what the indexing run wrote
    scoped = settings.model_copy(update={"benchmarks_dir": BENCHMARKS})
    app = make_app(scoped, Script(), graph=graph)
    async for http in client_for(app):
        grouped = by_approach(await ask(http, repository=name, approaches=["json-graph"]))

    assert graph.synced == [(main_ws, name)]  # not written again by Compare
    usage_event = next(e for e in grouped["json-graph"] if e["type"] == "usage")
    assert "already loaded" in usage_event["setup"]["description"]
