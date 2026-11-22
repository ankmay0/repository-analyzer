from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.tools import build_tools
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.workspace import Workspace


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
    assert sorted(tools) == ["get_repository_info", "list_repositories", "read_file", "search_code"]


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
