from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.github_tools import GitHubClient, GitHubSource, build_github_tools
from support import github_fixture_transport

FIXTURES = Path(__file__).resolve().parents[2] / "benchmarks" / "fixtures"


@pytest.fixture
def transport() -> tuple[httpx.MockTransport, list[httpx.Request]]:
    return github_fixture_transport(
        FIXTURES, owner="acme", repo="mono", ref="v1", prefix="benchmarks/fixtures"
    )


@pytest.fixture
def client(transport: tuple[httpx.MockTransport, list[httpx.Request]]) -> GitHubClient:
    return GitHubClient(token="t0ken", transport=transport[0])


@pytest.fixture
def tools(client: GitHubClient) -> dict[str, BaseTool]:
    sources = {
        name: GitHubSource("acme", "mono", "v1", f"benchmarks/fixtures/{name}")
        for name in ("shop-api", "shop-web")
    }
    return {t.name: t for t in build_github_tools(client, sources)}


def run(tool: BaseTool, **args: object) -> tuple[str, list[dict[str, object]]]:
    message = tool.invoke({"type": "tool_call", "id": "1", "name": tool.name, "args": args})
    return str(message.content), message.artifact


def test_list_files_is_scoped_to_the_fixture_and_skips_unindexable_files(
    tools: dict[str, BaseTool],
) -> None:
    text, _ = run(tools["list_files"], repo="shop-api")
    assert "app/routers/orders.py" in text
    assert "migrations/001_initial_schema.sql" in text
    assert "src/components" not in text  # shop-web files are not in shop-api
    text, _ = run(tools["list_files"], repo="shop-web", path="src/components")
    assert text.splitlines()[0] == "3 files in shop-web under src/components"


def test_read_file_returns_numbered_lines_and_a_source(tools: dict[str, BaseTool]) -> None:
    text, sources = run(
        tools["read_file"], repo="shop-api", path="app/services/pricing.py", end_line=7
    )
    assert text.splitlines()[0] == "shop-api/app/services/pricing.py lines 1-7 of 36"
    assert "    5  TAX_RATE = 0.0825" in text
    assert sources == [
        {"repo": "shop-api", "path": "app/services/pricing.py", "start_line": 1, "end_line": 7}
    ]


@pytest.mark.parametrize("path", ["../shop-web/package.json", "/etc/passwd", "app/nope.py", ""])
def test_read_file_refuses_paths_outside_the_listing(tools: dict[str, BaseTool], path: str) -> None:
    text, sources = run(tools["read_file"], repo="shop-api", path=path)
    assert text.startswith("Error: no such file")
    assert sources == []


def test_search_code_greps_files_fetched_from_github(tools: dict[str, BaseTool]) -> None:
    text, sources = run(tools["search_code"], query="FOR UPDATE", repo="shop-api")
    assert "shop-api/app/routers/orders.py:" in text
    assert "shop-api/app/routers/fulfillment.py:" in text
    assert all(s["repo"] == "shop-api" for s in sources)


def test_files_are_fetched_once_and_the_token_is_sent(
    tools: dict[str, BaseTool],
    client: GitHubClient,
    transport: tuple[httpx.MockTransport, list[httpx.Request]],
) -> None:
    run(tools["read_file"], repo="shop-api", path="app/cache.py")
    before = client.requests_made
    run(tools["read_file"], repo="shop-api", path="app/cache.py", start_line=3)
    assert client.requests_made == before
    requests = transport[1]
    assert all(r.headers["Authorization"] == "Bearer t0ken" for r in requests)
    assert requests[-1].headers["Accept"] == "application/vnd.github.raw+json"


def test_github_errors_and_unknown_repositories_become_tool_errors() -> None:
    denied = httpx.MockTransport(lambda _: httpx.Response(403, json={"message": "rate limited"}))
    tools = {
        t.name: t
        for t in build_github_tools(
            GitHubClient(transport=denied), {"shop-api": GitHubSource("a", "b")}
        )
    }
    text, _ = run(tools["list_files"], repo="shop-api")
    assert "HTTP 403" in text and "GITHUB_TOKEN" in text
    text, _ = run(tools["list_files"], repo="other")
    assert text == "Error: Unknown repository 'other'; known: shop-api"
