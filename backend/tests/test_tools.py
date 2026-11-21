from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.tools import build_graph_tools, build_rag_repository_tools
from repo_analyzer.config import Settings
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace


def call(tool: BaseTool, **args: Any) -> tuple[str, list[dict[str, Any]]]:
    """Invoke a tool the way an agent does and return ``(content, artifact)``."""
    message = tool.invoke({"type": "tool_call", "name": tool.name, "id": "t1", "args": args})
    return str(message.content), message.artifact or []


@pytest.fixture
def graph_tools(indexed_workspace: Workspace) -> dict[str, BaseTool]:
    return {t.name: t for t in build_graph_tools(indexed_workspace)}


@pytest.fixture
def repository_tools(settings: Settings, store: VectorStore, indexed_workspace: Workspace) -> Any:
    def build(repo: str) -> dict[str, BaseTool]:
        tools = build_rag_repository_tools(settings, store, indexed_workspace, repo)
        return {t.name: t for t in tools}

    return build


# --- hub tools: the dependency graph ------------------------------------------


def test_the_hub_has_no_tool_that_returns_code(graph_tools: dict[str, BaseTool]) -> None:
    assert set(graph_tools) == {"list_repositories", "analyze_impact"}


def test_list_repositories_shows_repos_and_edges(graph_tools: dict[str, BaseTool]) -> None:
    content, _ = call(graph_tools["list_repositories"])

    assert "- core-lib (" in content
    assert "mid-lib -> core-lib (runtime dependency on pypi package 'core-lib'" in content


def test_analyze_impact_reports_chains_and_strength(graph_tools: dict[str, BaseTool]) -> None:
    content, sources = call(graph_tools["analyze_impact"], repo="core-lib")

    assert "If core-lib fails, 3 indexed repositories are affected" in content
    assert "mid-lib (distance 1, runtime)" in content
    assert "web-app (distance 2, runtime)" in content
    assert "tools (distance 2, weak" in content
    assert {"repo": "mid-lib", "path": "pyproject.toml", "start_line": 1, "end_line": 1} in sources


def test_analyze_impact_for_a_leaf_and_for_an_unknown_repository(
    graph_tools: dict[str, BaseTool],
) -> None:
    leaf, _ = call(graph_tools["analyze_impact"], repo="tools")
    unknown, _ = call(graph_tools["analyze_impact"], repo="nope")

    assert "No indexed repository declares a dependency on tools" in leaf
    assert unknown.startswith("Error: Unknown repository 'nope'")


# --- expert tools in rag mode: one repository's code --------------------------------------


def test_repository_tools_do_not_take_a_repository_argument(repository_tools: Any) -> None:
    tools = repository_tools("mid-lib")

    assert set(tools) == {"search_code", "find_references", "read_file"}
    for tool in tools.values():
        assert "repo" not in tool.args  # the repository is fixed, not chosen by the model


def test_search_code_returns_cited_chunks_from_its_repository_only(
    repository_tools: Any,
) -> None:
    content, sources = call(
        repository_tools("mid-lib")["search_code"], query="create a login session for a user"
    )

    assert "[mid-lib/src/mid_lib/session.py:1-6]" in content
    assert "def create_session" in content
    assert {s["repo"] for s in sources} == {"mid-lib"}

    # The same query through another expert never surfaces mid-lib's code.
    _, other = call(repository_tools("web-app")["search_code"], query="create a login session")
    assert {s["repo"] for s in other} == {"web-app"}


def test_find_references_is_confined_to_its_repository(repository_tools: Any) -> None:
    core, core_sources = call(repository_tools("core-lib")["find_references"], text="sign_token")
    mid, _ = call(repository_tools("mid-lib")["find_references"], text="sign_token")
    web, _ = call(repository_tools("web-app")["find_references"], text="sign_token")

    assert "core-lib/src/core_lib/tokens.py:1: def sign_token" in core
    assert "mid-lib" not in core
    assert {s["repo"] for s in core_sources} == {"core-lib"}
    assert "mid-lib/src/mid_lib/session.py:1: from core_lib.tokens import sign_token" in mid
    assert web.startswith("No lines contain")


def test_read_file_returns_numbered_lines(repository_tools: Any) -> None:
    content, sources = call(
        repository_tools("mid-lib")["read_file"],
        path="src/mid_lib/session.py",
        start_line=4,
        end_line=5,
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
    repository_tools: Any, path: str
) -> None:
    content, sources = call(repository_tools("core-lib")["read_file"], path=path)

    assert content.startswith("Error:")
    assert "do-not-index" not in content and "root:" not in content and "mid_lib" not in content
    assert sources == []


def test_read_file_refuses_a_symlink_that_escapes_the_repository(
    repository_tools: Any, fixture_repos: dict[str, Path], tmp_path: Path
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("password = 'hunter2'\n")
    (fixture_repos["core-lib"] / "link.py").symlink_to(outside)

    content, _ = call(repository_tools("core-lib")["read_file"], path="link.py")

    assert content.startswith("Error:") and "hunter2" not in content


def test_tools_for_a_repository_that_is_no_longer_indexed_report_an_error(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    tools = {
        t.name: t for t in build_rag_repository_tools(settings, store, indexed_workspace, "gone")
    }

    for name, args in [
        ("search_code", {"query": "x"}),
        ("find_references", {"text": "x"}),
        ("read_file", {"path": "x.py"}),
    ]:
        content, _ = call(tools[name], **args)
        assert content.startswith("Error: Unknown repository 'gone'")
