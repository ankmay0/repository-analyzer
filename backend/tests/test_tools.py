from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.tools import build_tools
from repo_analyzer.config import Settings
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace


@pytest.fixture
def tools(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> dict[str, BaseTool]:
    return {t.name: t for t in build_tools(settings, store, indexed_workspace)}


def call(tool: BaseTool, **args: Any) -> tuple[str, list[dict[str, Any]]]:
    """Invoke a tool the way the agent does and return ``(content, artifact)``."""
    message = tool.invoke({"type": "tool_call", "name": tool.name, "id": "t1", "args": args})
    return str(message.content), message.artifact or []


def test_list_repositories_shows_repos_and_edges(tools: dict[str, BaseTool]) -> None:
    content, _ = call(tools["list_repositories"])

    assert "- core-lib (" in content
    assert "mid-lib -> core-lib (runtime dependency on pypi package 'core-lib'" in content


def test_analyze_impact_reports_chains_and_strength(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["analyze_impact"], repo="core-lib")

    assert "If core-lib fails, 3 indexed repositories are affected" in content
    assert "mid-lib (distance 1, runtime)" in content
    assert "web-app (distance 2, runtime)" in content
    assert "tools (distance 2, weak" in content
    assert {"repo": "mid-lib", "path": "pyproject.toml", "start_line": 1, "end_line": 1} in sources


def test_analyze_impact_for_a_leaf_and_for_an_unknown_repository(
    tools: dict[str, BaseTool],
) -> None:
    leaf, _ = call(tools["analyze_impact"], repo="tools")
    unknown, _ = call(tools["analyze_impact"], repo="nope")

    assert "No indexed repository declares a dependency on tools" in leaf
    assert unknown.startswith("Error: Unknown repository 'nope'")


def test_search_code_returns_cited_chunks(tools: dict[str, BaseTool]) -> None:
    content, sources = call(tools["search_code"], query="create a login session for a user")

    assert "[mid-lib/src/mid_lib/session.py:1-6]" in content
    assert "def create_session" in content
    assert sources[0]["repo"] == "mid-lib"

    _, scoped = call(tools["search_code"], query="session", repo="web-app")
    assert {s["repo"] for s in scoped} == {"web-app"}


def test_find_references_finds_exact_usages_across_repositories(
    tools: dict[str, BaseTool],
) -> None:
    content, sources = call(tools["find_references"], text="sign_token")

    assert "core-lib/src/core_lib/tokens.py:1: def sign_token" in content
    assert "mid-lib/src/mid_lib/session.py:1: from core_lib.tokens import sign_token" in content
    assert {s["repo"] for s in sources} == {"core-lib", "mid-lib"}

    missing, _ = call(tools["find_references"], text="does_not_exist_anywhere")
    assert missing.startswith("No lines contain")


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
