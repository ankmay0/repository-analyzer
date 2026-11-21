"""Agent tools in json mode: they read the workspace graph, never a vector store."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import BaseTool

from repo_analyzer.agent.tools import build_json_repository_tools, build_usage_tool
from repo_analyzer.config import Settings
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.workspace import Workspace
from test_tools import call


@pytest.fixture
def expert_tools(
    json_settings: Settings, json_workspace: Workspace, knowledge: KnowledgeStore
) -> Any:
    def build(repo: str) -> dict[str, BaseTool]:
        tools = build_json_repository_tools(json_settings, json_workspace, knowledge, repo)
        return {t.name: t for t in tools}

    return build


@pytest.fixture
def find_usages(json_workspace: Workspace, knowledge: KnowledgeStore) -> BaseTool:
    return build_usage_tool(json_workspace, knowledge)


# --- hub: cross-repository usages -----------------------------------------------


def test_find_usages_lists_importing_files_in_other_repositories(find_usages: BaseTool) -> None:
    content, sources = call(find_usages, repo="core-lib")

    assert content.startswith(
        "Static imports of code from core-lib by other repositories (mid-lib: 1, tools: 1):"
    )
    assert (
        "mid-lib/src/mid_lib/session.py:1 uses sign_token "
        "(defined at core-lib/src/core_lib/tokens.py:1)"
    ) in content
    assert "tools/scripts/smoke.py:1 uses sign_token" in content
    assert "imports module" not in content  # each importing line is reported once
    assert "core-lib/src" not in "".join(line.split(" ")[0] for line in content.splitlines()[1:])
    assert {s["repo"] for s in sources} == {"mid-lib", "tools"}


def test_find_usages_can_be_narrowed_to_one_symbol(find_usages: BaseTool) -> None:
    used, _ = call(find_usages, repo="core-lib", symbol="sign_token")
    unused, _ = call(find_usages, repo="core-lib", symbol="TokenError")
    leaf, _ = call(find_usages, repo="tools")
    unknown, _ = call(find_usages, repo="nope")

    assert "(mid-lib: 1, tools: 1)" in used and "imports module" not in used
    assert unused.startswith("No other indexed repository statically imports TokenError")
    assert leaf.startswith("No other indexed repository statically imports code from tools")
    assert unknown.startswith("Error: Unknown repository 'nope'")


# --- expert: one repository, from the graph only --------------------------------


def test_expert_tools_are_graph_backed_and_take_no_repository_argument(expert_tools: Any) -> None:
    tools = expert_tools("core-lib")

    assert set(tools) == {"repo_map", "find_symbol", "list_dependencies"}
    assert all("repo" not in tool.args for tool in tools.values())


def test_repo_map_lists_files_and_their_top_level_definitions(expert_tools: Any) -> None:
    content, _ = call(expert_tools("core-lib")["repo_map"])

    assert content.startswith("core-lib: 3 files\n")  # tokens.py, __init__.py, pyproject.toml
    assert "src/core_lib/tokens.py  (2 definitions)" in content
    assert "  function sign_token  :1" in content
    assert "  class TokenError  :6" in content
    assert "    def describe" not in content  # methods are not in the graph
    assert "pyproject.toml  (0 definitions)" in content and ".env" not in content

    narrowed, _ = call(expert_tools("core-lib")["repo_map"], path_prefix="src/core_lib/tok")
    assert narrowed.startswith("core-lib: 1 files under src/core_lib/tok")
    nothing, _ = call(expert_tools("core-lib")["repo_map"], path_prefix="nope/")
    assert nothing == "No files under 'nope/' in core-lib."


def test_find_symbol_gives_the_definition_and_who_imports_it(expert_tools: Any) -> None:
    content, sources = call(expert_tools("core-lib")["find_symbol"], name="SIGN_TOKEN")

    assert "function sign_token — core-lib/src/core_lib/tokens.py:1" in content
    assert "imported in this repository by: core-lib/src/core_lib/__init__.py:1" in content
    assert "mid-lib" not in content  # other repositories' usages are the hub's business
    assert sources == [
        {"repo": "core-lib", "path": "src/core_lib/tokens.py", "start_line": 1, "end_line": 1}
    ]

    partial, _ = call(expert_tools("core-lib")["find_symbol"], name="token")
    assert "sign_token" in partial and "TokenError" in partial
    missing, _ = call(expert_tools("core-lib")["find_symbol"], name="nothing_like_this")
    assert missing == "No definition matching 'nothing_like_this' in core-lib."


def test_list_dependencies_shows_what_a_file_imports_from_indexed_code(
    expert_tools: Any,
) -> None:
    content, sources = call(
        expert_tools("mid-lib")["list_dependencies"], path="src/mid_lib/session.py"
    )

    assert content.startswith("src/mid_lib/session.py depends on:")
    assert (
        "mid-lib/src/mid_lib/session.py:1 uses sign_token "
        "(defined at core-lib/src/core_lib/tokens.py:1)"
    ) in content
    assert {s["repo"] for s in sources} == {"core-lib"}  # points at the definition

    # A file that imports only the standard library has no tracked dependencies.
    none, _ = call(expert_tools("core-lib")["list_dependencies"], path="src/core_lib/tokens.py")
    assert "imports nothing from indexed code" in none

    missing, src = call(expert_tools("core-lib")["list_dependencies"], path="does/not/exist.py")
    assert missing.startswith("Error: no such file") and src == []


def test_graph_backed_tools_need_no_checkout(
    expert_tools: Any, fixture_repos: dict[str, Any]
) -> None:
    # Delete the clone: json mode works entirely from graph.json once indexed.
    for path in sorted(fixture_repos["mid-lib"].rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    tools = expert_tools("mid-lib")

    mapped, _ = call(tools["repo_map"])
    found, sources = call(tools["find_symbol"], name="create_session")

    assert "src/mid_lib/session.py" in mapped
    assert "function create_session — mid-lib/src/mid_lib/session.py:4" in found
    assert sources == [
        {"repo": "mid-lib", "path": "src/mid_lib/session.py", "start_line": 4, "end_line": 4}
    ]
