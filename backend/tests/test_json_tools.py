"""Agent tools in json mode: they read the pack, map and graph, never a vector store."""

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


# --- expert: one repository -----------------------------------------------------


def test_expert_tools_take_no_repository_argument(expert_tools: Any) -> None:
    tools = expert_tools("core-lib")

    assert set(tools) == {"repo_map", "find_symbol", "find_references", "read_file"}
    assert all("repo" not in tool.args for tool in tools.values())


def test_repo_map_shows_files_with_signatures_and_descriptions(expert_tools: Any) -> None:
    content, _ = call(expert_tools("core-lib")["repo_map"])

    assert content.startswith("core-lib: 3 files\n")
    assert "src/core_lib/tokens.py  (10 lines)" in content
    assert (
        "  def sign_token(payload: str, secret: str) -> str  :1"
        "  — Sign a payload and return the token."
    ) in content
    assert "  class TokenError(Exception)  :6  — Raised when a token cannot be verified." in content
    assert "    def describe(self) -> str  :9" in content
    assert "pyproject.toml" in content and ".env" not in content

    narrowed, _ = call(expert_tools("core-lib")["repo_map"], path_prefix="src/core_lib/tok")
    assert narrowed.startswith("core-lib: 1 files under src/core_lib/tok")
    nothing, _ = call(expert_tools("core-lib")["repo_map"], path_prefix="nope/")
    assert nothing == "No files under 'nope/' in core-lib."


def test_find_symbol_gives_the_definition_and_who_imports_it(expert_tools: Any) -> None:
    content, sources = call(expert_tools("core-lib")["find_symbol"], name="SIGN_TOKEN")

    assert (
        "core-lib/src/core_lib/tokens.py:1-3  def sign_token(payload: str, secret: str) -> str"
        in content
    )
    assert "imported in this repository by: core-lib/src/core_lib/__init__.py:1" in content
    assert "mid-lib" not in content  # other repositories' usages are the hub's business
    assert sources == [
        {"repo": "core-lib", "path": "src/core_lib/tokens.py", "start_line": 1, "end_line": 3}
    ]

    partial, _ = call(expert_tools("core-lib")["find_symbol"], name="token")
    assert "sign_token" in partial and "class TokenError" in partial
    missing, _ = call(expert_tools("core-lib")["find_symbol"], name="nothing_like_this")
    assert missing == "No definition matching 'nothing_like_this' in core-lib."


def test_find_references_and_read_file_are_served_from_the_pack(
    expert_tools: Any, fixture_repos: dict[str, Any]
) -> None:
    # Remove the checkout: json mode must not need it once the pack is written.
    for path in sorted(fixture_repos["mid-lib"].rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    tools = expert_tools("mid-lib")

    found, sources = call(tools["find_references"], text="sign_token")
    read, read_sources = call(
        tools["read_file"], path="src/mid_lib/session.py", start_line=4, end_line=5
    )

    assert "mid-lib/src/mid_lib/session.py:1: from core_lib.tokens import sign_token" in found
    assert {s["repo"] for s in sources} == {"mid-lib"}
    assert read.splitlines()[0] == "mid-lib/src/mid_lib/session.py lines 4-5 of 6"
    assert "    4  def create_session(user: str) -> str:" in read
    assert read_sources == [
        {"repo": "mid-lib", "path": "src/mid_lib/session.py", "start_line": 4, "end_line": 5}
    ]


@pytest.mark.parametrize(
    "path",
    ["../mid-lib/pyproject.toml", "/etc/passwd", ".env", "src", "missing.py", "../../etc/passwd"],
)
def test_read_file_only_serves_files_that_are_in_the_pack(expert_tools: Any, path: str) -> None:
    content, sources = call(expert_tools("core-lib")["read_file"], path=path)

    assert content.startswith("Error: no such file")
    assert "do-not-index" not in content and "root:" not in content
    assert sources == []
