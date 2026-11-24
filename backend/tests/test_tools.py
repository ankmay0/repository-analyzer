from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.tools import build_graph_tools, build_tools
from repo_analyzer.config import Settings
from repo_analyzer.domain import SourceFile
from repo_analyzer.ingestion.graph_extract import extract_graph
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.workspace import IndexedRepository, Workspace, WorkspaceState
from support import FakeGraphStore


@pytest.fixture
def tools(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> dict[str, BaseTool]:
    return {t.name: t for t in build_tools(settings, index_store, indexed_workspace)}


def call(tool: BaseTool, **args: Any) -> tuple[str, list[dict[str, Any]]]:
    """Invoke a tool the way the agent does and return ``(content, artifact)``."""
    message = tool.invoke({"type": "tool_call", "name": tool.name, "id": "t1", "args": args})
    return str(message.content), message.artifact or []


def test_the_agent_gets_exactly_the_four_tools(tools: dict[str, BaseTool]) -> None:
    assert sorted(tools) == [
        "get_repository_info",
        "list_repositories",
        "load_skill",
        "read_file",
        "search_code",
    ]


def test_list_repositories_shows_repos_and_their_sizes(tools: dict[str, BaseTool]) -> None:
    content, _ = call(tools["list_repositories"])

    assert "- core-lib (owner local," in content
    assert "1 functions/classes" in content
    assert "1 declared dependencies" in content  # mid-lib -> core-lib


def test_get_repository_info_returns_the_repository_json(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["get_repository_info"], repo="mid-lib")

    info = json.loads(content)
    assert info["name"] == "mid-lib"
    assert info["dependencies"] == [
        {"name": "core-lib", "ecosystem": "pypi", "kind": "runtime", "manifest": "pyproject.toml"}
    ]
    assert [f["path"] for f in info["files"]] == ["pyproject.toml", "src/mid_lib/session.py"]
    assert info["files"][1]["symbols"]["functions"][0]["name"] == "create_session"
    assert sources == [
        {"repo": "mid-lib", "path": "pyproject.toml", "start_line": 1, "end_line": 1}
    ]


def test_get_repository_info_can_be_limited_to_a_path(tools: dict[str, BaseTool]) -> None:
    content, _ = call(tools["get_repository_info"], repo="mid-lib", path="./src/")

    info = json.loads(content)
    assert info["path_filter"] == "src"
    assert [f["path"] for f in info["files"]] == ["src/mid_lib/session.py"]

    single, _ = call(tools["get_repository_info"], repo="mid-lib", path="pyproject.toml")
    assert [f["path"] for f in json.loads(single)["files"]] == ["pyproject.toml"]

    missing, _ = call(tools["get_repository_info"], repo="mid-lib", path="nope")
    assert missing.startswith("Error: no indexed files under 'nope'")


def test_get_repository_info_for_an_unknown_repository(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["get_repository_info"], repo="nope")

    assert content.startswith("Error: Unknown repository 'nope'")
    assert sources == []


def test_get_repository_info_shrinks_a_huge_index_and_says_so(
    settings: Settings,
    index_store: RepoIndexStore,
    indexed_workspace: Workspace,
    tools: dict[str, BaseTool],
) -> None:
    index = index_store.load("local__core-lib.json")
    template = index["files"][1]
    index["files"] = [
        {**template, "path": f"pkg/module_{n:04d}.py"} for n in range(1_500)
    ]  # each carries symbols
    index_store.save(index)

    content, _ = call(tools["get_repository_info"], repo="core-lib")

    info = json.loads(content)
    assert len(content) <= 40_000
    assert "narrow it with `path`" in info["note"] or "pass `path`" in info["note"]
    assert all("symbols" not in f for f in info["files"])

    scoped, _ = call(tools["get_repository_info"], repo="core-lib", path="pkg/module_0007.py")
    assert json.loads(scoped)["files"][0]["symbols"]["functions"]


def test_search_code_finds_exact_text_across_repositories(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["search_code"], query="sign_token")

    assert "core-lib/src/core_lib/tokens.py:1: def sign_token" in content
    assert "mid-lib/src/mid_lib/session.py:1: from core_lib.tokens import sign_token" in content
    assert {s["repo"] for s in sources} == {"core-lib", "mid-lib"}

    missing, _ = call(tools["search_code"], query="does_not_exist_anywhere")
    assert missing.startswith("No lines match")


def test_search_code_can_be_scoped_and_case_insensitive(tools: dict[str, BaseTool]) -> None:
    _, scoped = call(tools["search_code"], query="sign_token", repo="mid-lib")
    assert {s["repo"] for s in scoped} == {"mid-lib"}

    _, under_path = call(tools["search_code"], query="sign_token", path="src/core_lib")
    assert {s["path"] for s in under_path} == {"src/core_lib/tokens.py"}

    assert call(tools["search_code"], query="SIGN_TOKEN")[0].startswith("No lines match")
    insensitive, _ = call(tools["search_code"], query="SIGN_TOKEN", ignore_case=True)
    assert "def sign_token" in insensitive


def test_search_code_supports_regular_expressions(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["search_code"], query=r"^def \w+_session\(", regex=True)

    assert content.splitlines()[0] == "1 matching lines:"
    assert sources == [
        {"repo": "mid-lib", "path": "src/mid_lib/session.py", "start_line": 4, "end_line": 4}
    ]

    invalid, _ = call(tools["search_code"], query="(unclosed", regex=True)
    assert invalid.startswith("Error: invalid regular expression")
    too_long, _ = call(tools["search_code"], query="a" * 301, regex=True)
    assert too_long.startswith("Error: regex is longer")
    assert call(tools["search_code"], query="  ")[0] == "Error: query must not be empty"


def test_search_code_never_searches_secrets_or_symlinks(
    tools: dict[str, BaseTool], fixture_repos: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("password = 'hunter2'\n")
    (fixture_repos["core-lib"] / "link.py").symlink_to(outside)

    assert call(tools["search_code"], query="do-not-index")[0].startswith("No lines match")
    assert call(tools["search_code"], query="hunter2")[0].startswith("No lines match")


def test_search_code_caps_the_number_of_hits(
    tools: dict[str, BaseTool], fixture_repos: dict[str, Path]
) -> None:
    (fixture_repos["core-lib"] / "many.py").write_text("x = 1\n" * 100)

    content, sources = call(tools["search_code"], query="x = 1")

    assert content.splitlines()[0].startswith("100 matching lines (showing the first 60")
    assert len(sources) == 60


def test_read_file_returns_numbered_lines(tools: dict[str, BaseTool]) -> None:
    content, sources = call(
        tools["read_file"], repo="mid-lib", path="src/mid_lib/session.py", start_line=4, end_line=5
    )

    assert content.splitlines()[0] == "mid-lib/src/mid_lib/session.py lines 4-5 of 6"
    assert "    4  def create_session(user: str) -> str:" in content
    assert sources == [
        {"repo": "mid-lib", "path": "src/mid_lib/session.py", "start_line": 4, "end_line": 5}
    ]


@pytest.mark.parametrize(
    "path",
    [
        "../mid-lib/pyproject.toml",  # traversal into a sibling repository
        "../../../../../../etc/passwd",
        "/etc/passwd",
        ".env",  # exists in the fixture, but is a secret
        "src",  # a directory
        "missing.py",
    ],
)
def test_read_file_refuses_paths_outside_the_indexable_tree(
    tools: dict[str, BaseTool], path: str
) -> None:
    content, sources = call(tools["read_file"], repo="core-lib", path=path)

    assert content.startswith("Error:")
    assert "do-not-index" not in content and "root:" not in content
    assert sources == []


def test_read_file_refuses_a_symlink_that_escapes_the_repository(
    tools: dict[str, BaseTool], fixture_repos: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("password = 'hunter2'\n")
    (fixture_repos["core-lib"] / "link.py").symlink_to(outside)

    content, _ = call(tools["read_file"], repo="core-lib", path="link.py")

    assert content.startswith("Error:") and "hunter2" not in content


def test_load_skill_returns_the_body_without_frontmatter(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["load_skill"], name="repo-navigation")

    assert content.startswith("# Repository navigation")
    assert "list_repositories" in content
    assert "name: repo-navigation" not in content
    assert sources == []


def test_load_skill_unknown_name_lists_the_valid_ones(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["load_skill"], name="no-such-skill")

    assert content.startswith("Error: Unknown skill 'no-such-skill'.")
    assert "Available skills:" in content
    assert "repo-navigation" in content and "dependency-review" in content
    assert sources == []


@pytest.mark.parametrize(
    "name",
    ["../secrets", "foo/../../etc/passwd", "repo-navigation/SKILL.md", ""],
)
def test_load_skill_rejects_path_traversal_and_invalid_names(
    tools: dict[str, BaseTool], name: str
) -> None:
    content, sources = call(tools["load_skill"], name=name)

    assert content.startswith("Error: Unknown skill")
    assert "Available skills:" in content
    assert sources == []
    # The tool must not echo file contents from outside the catalogue.
    assert "ANTHROPIC_API_KEY" not in content


# --- code graph tools ---------------------------------------------------------------

SHOP_API = Path(__file__).resolve().parents[2] / "benchmarks" / "fixtures" / "shop-api"


def graph_setup(
    tmp_path: Path, name: str, files: list[SourceFile] | None = None
) -> tuple[dict[str, BaseTool], FakeGraphStore]:
    """Graph tools over one repository loaded into an in-memory store."""
    workspace = Workspace(tmp_path / f"{name}-workspace.json")
    root = str(SHOP_API)
    workspace.replace(
        WorkspaceState(
            (IndexedRepository(name, "local", root, root, None, None, 0, 0, 0, f"{name}.json"),)
        )
    )
    if files is None:
        files = list(scan_repository(name, SHOP_API, max_file_bytes=200_000))
    store = FakeGraphStore()
    store.sync_repo("ws", name, None, extract_graph(files))
    return {t.name: t for t in build_graph_tools(store, workspace, "ws")}, store


@pytest.fixture
def graph(tmp_path: Path) -> dict[str, BaseTool]:
    return graph_setup(tmp_path, "shop-api")[0]


def test_trace_endpoint_lists_the_call_path_with_ranges_and_tables(
    graph: dict[str, BaseTool],
) -> None:
    content, sources = call(graph["trace_endpoint"], repo="shop-api", method="POST", path="/orders")

    assert (
        "POST /orders in shop-api (route declared at shop-api/app/routers/orders.py:33)" in content
    )
    assert "0. create_order  shop-api/app/routers/orders.py:33-84\n" in content
    assert (
        "  1. price_order  shop-api/app/services/pricing.py:20-32  [called at line 56]" in content
    )
    assert "    2. cents_to_dollars  shop-api/app/services/pricing.py:35-36" in content
    assert "writes table order_items (line 75)" in content
    assert "reads table customers (line 37)" in content
    assert "Gaps: none recorded" in content
    assert sources[0] == {
        "repo": "shop-api", "path": "app/routers/orders.py", "start_line": 33, "end_line": 84,
    }  # fmt: skip
    assert len(sources) == 6  # one per step


def test_trace_endpoint_follows_references_and_matches_concrete_paths(
    graph: dict[str, BaseTool],
) -> None:
    products, _ = call(graph["trace_endpoint"], repo="shop-api", method="get", path="/products/")
    summary, _ = call(
        graph["trace_endpoint"], repo="shop-api", method="GET", path="/customers/7/summary"
    )

    assert "_load_active_products" in products and "[passed as a function at line 17]" in products
    assert "reads table products" in products
    assert "GET /customers/{customer_id}/summary" in summary
    assert "_load_summary" in summary and "reads table orders" in summary


def test_trace_endpoint_without_a_match_lists_the_known_endpoints(
    graph: dict[str, BaseTool],
) -> None:
    content, sources = call(graph["trace_endpoint"], repo="shop-api", method="DELETE", path="/x")

    assert content.startswith("No endpoint matches DELETE /x in shop-api.")
    assert "- GET /products" in content and "- POST /orders/{order_id}/fulfill" in content
    assert sources == []


def test_graph_tools_reject_unknown_repositories(graph: dict[str, BaseTool]) -> None:
    content, _ = call(graph["trace_endpoint"], repo="nope", method="GET", path="/")
    callers, _ = call(graph["callers_of"], repo="nope", function="x")

    assert content.startswith("Error: Unknown repository 'nope'")
    assert callers.startswith("Error: Unknown repository 'nope'")


def test_a_graph_outage_becomes_an_error_the_model_can_work_around(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools, store = graph_setup(tmp_path, "shop-api")

    def down(*_: object) -> None:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(store, "trace_endpoint", down)
    monkeypatch.setattr(store, "neighbours", down)

    content, _ = call(tools["trace_endpoint"], repo="shop-api", method="GET", path="/products")
    callers, _ = call(tools["callers_of"], repo="shop-api", function="to_order")

    for text in (content, callers):
        assert text.startswith("Error: the code graph could not be queried (ConnectionError)")
        assert "search_code" in text


def test_callers_and_callees(graph: dict[str, BaseTool]) -> None:
    callers, sources = call(graph["callers_of"], repo="shop-api", function="to_order")
    callees, _ = call(graph["callees_of"], repo="shop-api", function="list_products")
    missing, _ = call(graph["callers_of"], repo="shop-api", function="nope")

    assert callers.startswith("4 callers of to_order in shop-api:")
    assert "- create_order  shop-api/app/routers/orders.py:33-84  (calls it at line 84)" in callers
    assert {s["path"] for s in sources} == {"app/routers/orders.py"}
    assert "_load_active_products" in callees and "passed as a function" in callees
    assert missing.startswith("No callers of 'nope' in the code graph for shop-api.")


def test_trace_reports_gaps_and_paths_without_tables(tmp_path: Path) -> None:
    def py(path: str, text: str) -> SourceFile:
        return SourceFile("svc", path, "python", text)

    tools, _ = graph_setup(
        tmp_path,
        "svc",
        [
            py(
                "main.py",
                "from fastapi import FastAPI\napp = FastAPI()\n\n"
                '@app.get("/ping")\ndef ping():\n    return helper()\n',
            ),
            py("a.py", "def helper(): ...\n"),
            py("b.py", "def helper(): ...\n"),
        ],
    )

    content, _ = call(tools["trace_endpoint"], repo="svc", method="GET", path="/ping")

    assert "Gaps (calls that matched several functions" in content
    assert "ping: helper" in content
    assert "No table access was found on this path" in content
