"""Direct LangChain agent: file tools, sandboxing, the HTTP endpoint and no JSON index."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from repo_analyzer.api.app import create_app
from repo_analyzer.config import Settings
from repo_analyzer.direct_agent import (
    ask_direct,
    build_direct_tools,
    resolve_checkout,
    resolve_under_root,
)
from repo_analyzer.errors import InvalidRepositorySourceError, UnknownRepositoryError
from repo_analyzer.ingestion.fetcher import RepoFetcher
from support import ScriptedChatModel, tool_call


def tool_by_name(tools: list[BaseTool], name: str) -> BaseTool:
    return next(t for t in tools if t.name == name)


def invoke(tools: list[BaseTool], name: str, **kwargs: object) -> str:
    return str(tool_by_name(tools, name).invoke(kwargs))


@pytest.fixture
def repo(fixture_repos: dict[str, Path]) -> Path:
    return fixture_repos["core-lib"]


@pytest.fixture
def tools(repo: Path, settings: Settings) -> list[BaseTool]:
    return build_direct_tools(repo, max_file_bytes=settings.max_file_bytes)


def test_list_read_and_search_work_on_a_fixture(tools: list[BaseTool]) -> None:
    listing = invoke(tools, "list_files")
    assert "src/core_lib/tokens.py" in listing
    assert "pyproject.toml" in listing
    assert ".env" not in listing  # secrets are not listed

    tree = invoke(tools, "repo_tree")
    assert "tokens.py" in tree
    assert ".git" not in tree
    assert "node_modules" not in tree

    body = invoke(tools, "read_file", path="src/core_lib/tokens.py")
    assert "def sign_token" in body
    assert "    1  " in body

    hits = invoke(tools, "search_code", pattern="sign_token")
    assert "src/core_lib/tokens.py:" in hits
    assert "sign_token" in hits


def test_search_respects_glob_and_rejects_a_bad_regex(tools: list[BaseTool]) -> None:
    py_hits = invoke(tools, "search_code", pattern="sign_token", glob="*.py")
    assert "tokens.py" in py_hits
    toml_hits = invoke(tools, "search_code", pattern="sign_token", glob="*.toml")
    assert "No lines match" in toml_hits
    assert "Error: invalid regular expression" in invoke(tools, "search_code", pattern="(")


def test_sandbox_rejects_paths_outside_the_clone(tools: list[BaseTool], repo: Path) -> None:
    for path in ("../secret.txt", "/etc/passwd", "C:/Windows/system.ini", ".."):
        for name, kwargs in (
            ("read_file", {"path": path}),
            ("list_files", {"path": path}),
        ):
            result = invoke(tools, name, **kwargs)
            assert result.startswith("Error:"), (name, path, result)

    # The helper itself also rejects these, including a resolved ".." escape.
    for path in ("../secret.txt", "/etc/passwd"):
        resolved = resolve_under_root(repo, path)
        assert isinstance(resolved, str) and resolved.startswith("Error:")


def test_sandbox_skips_git_node_modules_binaries_and_secrets(
    tmp_path: Path, settings: Settings
) -> None:
    root = tmp_path / "sample"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("module.exports = 1\n")
    (root / "logo.bin").write_bytes(b"\x00\x01\x02binary")
    (root / ".env").write_text("SECRET=do-not-read\n")
    outside = tmp_path / "outside.txt"
    outside.write_text("leaked\n")
    (root / "link.py").symlink_to(outside)

    tools = build_direct_tools(root, max_file_bytes=settings.max_file_bytes)

    listing = invoke(tools, "list_files")
    assert "src/app.py" in listing
    assert ".git" not in listing
    assert "node_modules" not in listing
    assert "logo.bin" not in listing
    assert ".env" not in listing
    assert "link.py" not in listing

    assert "skipped directory" in invoke(tools, "read_file", path=".git/config")
    assert "skipped directory" in invoke(tools, "read_file", path="node_modules/pkg/index.js")
    assert "not a readable source file" in invoke(tools, "read_file", path="logo.bin")
    assert "not a readable source file" in invoke(tools, "read_file", path=".env")
    # The symlink resolves outside the clone.
    assert "escapes" in invoke(tools, "read_file", path="link.py")

    hits = invoke(tools, "search_code", pattern="module.exports|SECRET=|leaked|VALUE")
    assert "src/app.py" in hits
    assert "node_modules" not in hits
    assert ".env" not in hits
    assert "leaked" not in hits


def test_read_file_caps_size_and_line_range(tmp_path: Path) -> None:
    root = tmp_path / "sized"
    root.mkdir()
    (root / "small.py").write_text("\n".join(f"line_{i}" for i in range(1, 21)) + "\n")
    (root / "huge.py").write_text("x = 1\n" * 100)
    tools = build_direct_tools(root, max_file_bytes=10_000)
    body = invoke(tools, "read_file", path="small.py", start_line=2, end_line=4)
    assert "line_2" in body and "line_4" in body
    assert "line_5" not in body
    tiny = build_direct_tools(root, max_file_bytes=20)
    assert "too large to read" in invoke(tiny, "read_file", path="huge.py")


def test_resolve_checkout_uses_a_local_path_and_an_existing_name(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    fetcher = RepoFetcher(
        settings.checkouts_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=True,
    )
    snapshot = resolve_checkout(str(fixture_repos["core-lib"]), fetcher)
    assert snapshot.name == "core-lib"
    assert snapshot.root == fixture_repos["core-lib"].resolve()

    # A bare name hits a folder already sitting in the checkouts directory.
    dest = settings.checkouts_dir / "core-lib"
    dest.mkdir(parents=True)
    (dest / "readme.md").write_text("cached clone\n")
    named = resolve_checkout("core-lib", fetcher)
    assert named.root == dest
    assert named.name == "core-lib"

    with pytest.raises(UnknownRepositoryError, match="No checkout named"):
        resolve_checkout("missing-repo", fetcher)
    with pytest.raises(InvalidRepositorySourceError):
        resolve_checkout("   ", fetcher)


@pytest.mark.asyncio
async def test_ask_direct_uses_the_files_and_does_not_write_an_index(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    fetcher = RepoFetcher(
        settings.checkouts_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=True,
    )
    model = ScriptedChatModel(
        responses=[
            tool_call("search_code", {"pattern": "sign_token"}),
            AIMessage(content="core-lib signs tokens in src/core_lib/tokens.py."),
        ]
    )

    result = await ask_direct(
        str(fixture_repos["core-lib"]),
        "Where is sign_token defined?",
        settings=settings,
        fetcher=fetcher,
        model=model,
    )

    assert result.repo == "core-lib"
    assert "tokens.py" in result.answer
    assert result.tool_calls[0]["name"] == "search_code"
    assert "sign_token" in (result.tool_calls[0]["result"] or "")
    # The point of this mode: no JSON index is created.
    assert not settings.repos_dir.exists() or not any(settings.repos_dir.glob("*.json"))


@pytest.fixture
def app(settings: Settings, model: ScriptedChatModel) -> FastAPI:
    return create_app(settings, model_factory=lambda: model)


@pytest.fixture
def model() -> ScriptedChatModel:
    return ScriptedChatModel(
        responses=[
            tool_call("read_file", {"path": "src/core_lib/tokens.py"}),
            AIMessage(content="It signs a payload and returns a token."),
        ]
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_direct_ask_endpoint_answers_without_indexing(
    client: httpx.AsyncClient, fixture_repos: dict[str, Path], settings: Settings
) -> None:
    response = await client.post(
        "/api/direct/ask",
        json={
            "repo": str(fixture_repos["core-lib"]),
            "question": "What does sign_token do?",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["repo"] == "core-lib"
    assert body["answer"] == "It signs a payload and returns a token."
    assert body["tool_calls"][0]["name"] == "read_file"
    assert "def sign_token" in (body["tool_calls"][0]["result"] or "")
    assert not settings.repos_dir.exists() or not any(settings.repos_dir.glob("*.json"))


async def test_direct_ask_endpoint_rejects_a_bad_repo(client: httpx.AsyncClient) -> None:
    missing = await client.post(
        "/api/direct/ask", json={"repo": "no-such-repo", "question": "Hello?"}
    )
    assert missing.status_code == 404
    bad_url = await client.post(
        "/api/direct/ask",
        json={"repo": "https://evil.example.com/org/repo", "question": "Hello?"},
    )
    assert bad_url.status_code == 400


async def test_direct_ask_without_credentials_is_a_503(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    def no_model() -> ScriptedChatModel:
        from repo_analyzer.errors import LLMNotConfiguredError

        raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set on the server")

    app = create_app(settings, model_factory=no_model)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/direct/ask",
            json={"repo": str(fixture_repos["core-lib"]), "question": "What is this?"},
        )
    assert response.status_code == 503
    assert "ANTHROPIC_API_KEY" in response.json()["detail"]
