"""MCP server tests: a FastMCP in-memory client, the real backend, a scripted model.

No API key, Neo4j or network is needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastmcp import Client
from langchain_core.messages import AIMessage

from repo_analyzer import mcp_server
from repo_analyzer.api.services import Services, build_services
from repo_analyzer.config import Settings
from support import FakeGraphStore, ScriptedChatModel


def make_services(settings: Settings, graph_store: FakeGraphStore | None = None) -> Services:
    model = ScriptedChatModel(responses=[AIMessage(content="It is a small library.")])
    return build_services(settings, model_factory=lambda: model, graph_store=graph_store)


@pytest.fixture
def services(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Services:
    built = make_services(settings)
    monkeypatch.setattr(mcp_server, "get_services", lambda: built)
    return built


async def test_lists_the_expected_tools() -> None:
    async with Client(mcp_server.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names == {"list_repositories", "index_repositories", "ask_question", "trace_endpoint"}


async def test_index_list_and_ask(services: Services, fixture_repos: dict[str, Path]) -> None:
    async with Client(mcp_server.mcp) as client:
        before = await client.call_tool("ask_question", {"question": "What is this?"})
        assert "index_repositories" in before.data  # asked too early

        sources = [str(path) for path in fixture_repos.values()]
        done = await client.call_tool("index_repositories", {"repositories": sources})
        assert done.data == "Indexed 4 repositories"

        repos = await client.call_tool("list_repositories", {})
        assert [r["name"] for r in repos.data] == ["core-lib", "mid-lib", "web-app", "tools"]

        answer = await client.call_tool("ask_question", {"question": "What is this?"})
        assert answer.data == "It is a small library."


async def test_index_rejects_bad_input(services: Services) -> None:
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("index_repositories", {"repositories": ["not a url"]})
    assert result.data.startswith("Error:")


async def test_trace_endpoint_needs_the_graph(services: Services) -> None:
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool(
            "trace_endpoint", {"repo": "web-app", "method": "GET", "path": "/x"}
        )
    assert "graph is disabled" in result.data


async def test_trace_endpoint_with_a_graph(
    settings: Settings, fixture_repos: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    graph_settings = settings.model_copy(update={"graph_enabled": True})
    built = make_services(graph_settings, FakeGraphStore())
    monkeypatch.setattr(mcp_server, "get_services", lambda: built)
    async with Client(mcp_server.mcp) as client:
        await client.call_tool(
            "index_repositories", {"repositories": [str(p) for p in fixture_repos.values()]}
        )
        result = await client.call_tool(
            "trace_endpoint", {"repo": "web-app", "method": "GET", "path": "/nope"}
        )
    assert "No endpoints are in the code graph" in result.data  # the graph tool answered


async def test_status_resource(services: Services) -> None:
    async with Client(mcp_server.mcp) as client:
        contents = await client.read_resource("info://status")
    status = json.loads(contents[0].text)
    assert status["ctags_available"] is True
    assert status["graph_available"] is False
