from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage

from repo_analyzer.agent.skills import SkillLibrary, default_skills_dir
from repo_analyzer.api.app import create_app
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError
from support import ScriptedChatModel, tool_call


@pytest.fixture
def model() -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[
            tool_call("search_code", {"query": "sign_token"}),
            AIMessage(content="It is used in mid-lib."),
        ]
    )


@pytest.fixture
def app(settings: Settings, model: ScriptedChatModel) -> FastAPI:
    return create_app(settings, model_factory=lambda: model)


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
    assert "embedder" not in body
    assert body["llm_model"] == "claude-opus-5-5"
    assert body["ctags_available"] is True
    assert body["ctags_version"] is not None and "Universal Ctags" in body["ctags_version"]


async def test_index_then_inspect_workspace_and_json_files(
    client: httpx.AsyncClient, app: FastAPI, fixture_repos: dict[str, Path], settings: Settings
) -> None:
    assert (await client.get("/api/workspace")).json()["repositories"] == []

    await index(client, app, [str(p) for p in fixture_repos.values()])

    workspace = (await client.get("/api/workspace")).json()
    assert workspace["job"]["state"] == "succeeded"
    assert workspace["job"]["error"] is None
    assert [r["name"] for r in workspace["repositories"]] == [
        "core-lib", "mid-lib", "web-app", "tools",
    ]  # fmt: skip
    mid = workspace["repositories"][1]
    assert mid["owner"] == "local"
    assert (mid["file_count"], mid["symbol_count"], mid["dependency_count"]) == (2, 1, 1)
    assert mid["index_file"] == "local__mid-lib.json"
    assert "root" not in mid  # server paths are not exposed
    assert "edges" not in workspace and "chunk_count" not in mid

    assert sorted(p.name for p in settings.repos_dir.glob("*.json")) == [
        "local__core-lib.json",
        "local__mid-lib.json",
        "local__tools.json",
        "local__web-app.json",
    ]
    assert settings.workspace_file.is_file()


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
        "tool_call",
        "sources",
        "token",
        "token",
        "usage",
        "done",
    ]
    assert events[0][1] == {"name": "search_code", "args": {"query": "sign_token"}}
    assert {s["repo"] for s in events[1][1]["sources"]} == {"core-lib", "mid-lib"}
    assert "".join(d["text"] for n, d in events if n == "token") == "It is used in mid-lib."


async def test_chat_requires_an_indexed_workspace(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/chat", json={"thread_id": "t", "message": "hi"})

    assert response.status_code == 409
    assert "Index at least one repository" in response.json()["detail"]


async def test_chat_without_credentials_is_a_503_not_a_broken_stream(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    def no_model() -> ScriptedChatModel:
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


async def test_the_impact_endpoint_is_gone(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/workspace/impact/core-lib")).status_code == 404


async def test_health_reports_when_ctags_is_unavailable(
    settings: Settings, model: ScriptedChatModel
) -> None:
    settings.ctags_path = "/no/such/ctags"
    app = create_app(settings, model_factory=lambda: model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        body = (await http.get("/api/health")).json()

    assert body["ctags_available"] is False
    assert body["ctags_version"] is None


async def test_index_without_ctags_is_a_503(
    settings: Settings, model: ScriptedChatModel, fixture_repos: dict[str, Path]
) -> None:
    settings.ctags_path = "/no/such/ctags"
    app = create_app(settings, model_factory=lambda: model)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        response = await http.post(
            "/api/workspace/index", json={"repositories": [str(fixture_repos["core-lib"])]}
        )

        assert response.status_code == 503
        assert "Universal Ctags is required" in response.json()["detail"]
        assert (await http.get("/api/workspace")).json()["job"]["state"] == "idle"


async def test_list_skills(client: httpx.AsyncClient) -> None:
    body = (await client.get("/api/skills")).json()

    names = [item["name"] for item in body]
    assert names == sorted(SkillLibrary.load(default_skills_dir()).names())
    review = next(item for item in body if item["name"] == "dependency-review")
    assert "dependencies" in review["description"]
    assert "body" not in review
