from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from repo_analyzer.api.app import create_app
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError
from support import ScriptedChatModel, consult, searching_expert


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    hub = ScriptedChatModel(
        responses=[
            consult("mid-lib", "Where is sign_token used?"),
            AIMessage(content="It is used in mid-lib."),
        ]
    )
    expert = ScriptedChatModel(responder=searching_expert())
    return create_app(settings, model_factory=lambda role: hub if role == "hub" else expert)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def index(client: httpx.AsyncClient, app: FastAPI, sources: list[str]) -> None:
    response = await client.post("/api/workspace/index", json={"repositories": sources})
    assert response.status_code == 202, response.text
    assert response.json()["job"]["state"] == "running"
    await app.state.services.jobs.wait()


def parse_sse(body: str) -> list[tuple[str, dict]]:  # type: ignore[type-arg]
    events = []
    for block in body.replace("\r\n", "\n").strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.split("\n") if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


async def test_health(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/health")).json()

    assert body["status"] == "ok"
    assert body["embedder"] == "hashing:512"
    assert body["llm_model"] == "claude-opus-5-5"


async def test_index_then_inspect_workspace_and_impact(
    client: httpx.AsyncClient, app: FastAPI, fixture_repos: dict[str, Path]
) -> None:
    assert (await client.get("/api/workspace")).json()["repositories"] == []

    await index(client, app, [str(p) for p in fixture_repos.values()])

    workspace = (await client.get("/api/workspace")).json()
    assert workspace["job"]["state"] == "succeeded"
    assert workspace["job"]["error"] is None
    assert [r["name"] for r in workspace["repositories"]] == [
        "core-lib", "mid-lib", "web-app", "tools",
    ]  # fmt: skip
    assert "root" not in workspace["repositories"][0]  # server paths are not exposed
    assert len(workspace["edges"]) == 4

    impact = (await client.get("/api/workspace/impact/core-lib")).json()
    assert [(i["repo"], i["distance"], i["weak"]) for i in impact["impacted"]] == [
        ("mid-lib", 1, False),
        ("web-app", 2, False),
        ("tools", 2, True),
    ]
    assert [e["source_repo"] for e in impact["impacted"][1]["path"]] == ["web-app", "mid-lib"]


async def test_chat_streams_tool_activity_sources_and_the_answer(
    client: httpx.AsyncClient, app: FastAPI, fixture_repos: dict[str, Path]
) -> None:
    await index(client, app, [str(p) for p in fixture_repos.values()])

    response = await client.post(
        "/api/chat", json={"thread_id": "t-1", "message": "Who uses sign_token?"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(response.text)
    assert [name for name, _ in events] == [
        "tool_call",  # the hub delegates
        "expert_step",  # the expert searches its repository
        "sources",  # what the expert found
        "token",
        "token",
        "done",
    ]
    assert events[0][1] == {
        "name": "consult_repository",
        "args": {"repo": "mid-lib", "question": "Where is sign_token used?"},
    }
    assert events[1][1] == {
        "repo": "mid-lib",
        "name": "find_references",
        "args": {"text": "sign_token"},
    }
    assert {s["repo"] for s in events[2][1]["sources"]} == {"mid-lib"}
    assert "".join(d["text"] for n, d in events if n == "token") == "It is used in mid-lib."


async def test_chat_requires_an_indexed_workspace(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"thread_id": "t", "message": "hi"})

    assert response.status_code == 409
    assert "Index at least one repository" in response.json()["detail"]


async def test_chat_without_credentials_is_a_503_not_a_broken_stream(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    def no_model(role: str) -> ScriptedChatModel:
        raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set on the server")

    app = create_app(settings, model_factory=no_model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        await index(http, app, [str(fixture_repos["core-lib"])])
        response = await http.post("/api/chat", json={"thread_id": "t", "message": "hi"})

    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"thread_id": "has spaces", "message": "hi"},
        {"thread_id": "t", "message": ""},
        {"thread_id": "t", "message": "x" * 8_001},
        {"message": "hi"},
    ],
)
async def test_chat_validates_its_input(client: httpx.AsyncClient, payload: dict) -> None:  # type: ignore[type-arg]
    assert (await client.post("/api/chat", json=payload)).status_code == 422


async def test_invalid_sources_are_rejected_before_a_job_starts(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/workspace/index", json={"repositories": ["file:///etc/passwd"]}
    )

    assert response.status_code == 400
    assert (await client.get("/api/workspace")).json()["job"]["state"] == "idle"
    assert (await client.post("/api/workspace/index", json={"repositories": []})).status_code == 422


async def test_a_failed_job_reports_its_error_and_keeps_the_previous_workspace(
    client: httpx.AsyncClient, app: FastAPI, fixture_repos: dict[str, Path], tmp_path: Path
) -> None:
    await index(client, app, [str(fixture_repos["core-lib"])])
    vanishing = tmp_path / "vanishing"
    vanishing.mkdir()

    # Passes validation, then disappears before the worker reaches it.
    response = await client.post(
        "/api/workspace/index",
        json={"repositories": [str(fixture_repos["mid-lib"]), str(vanishing)]},
    )
    vanishing.rmdir()
    assert response.status_code == 202
    await app.state.services.jobs.wait()

    workspace = (await client.get("/api/workspace")).json()
    assert workspace["job"]["state"] == "failed"
    assert "Not a directory" in workspace["job"]["error"]
    assert [r["name"] for r in workspace["repositories"]] == ["core-lib"]


async def test_impact_for_unknown_repository_is_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/workspace/impact/nope")

    assert response.status_code == 404
    assert "Unknown repository" in response.json()["detail"]
