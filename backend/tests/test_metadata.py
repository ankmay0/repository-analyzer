from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from repo_analyzer.agent.tools import build_tools
from repo_analyzer.config import Settings
from repo_analyzer.domain import SourceFile
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.metadata import (
    MetadataGenerationError,
    MetadataStore,
    generate_endpoint_metadata,
    metadata_filename,
    parse_metadata_response,
    select_source_files,
)
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndex, RepoIndexStore
from repo_analyzer.workspace import Workspace
from support import FunctionChatModel, usage

ENTRY = {
    "kind": "endpoint",
    "name": "GET /customers/{id}/summary",
    "handler": "app/routers/customers.py:get_customer_summary",
    "location": "app/routers/customers.py:26",
    "data_source": "Aggregate over the orders table",
    "source_types": ["database", "cache", "quantum"],
    "tables": ["orders"],
    "caching": "60 s in-process TTL per customer",
    "transformations": "cents to dollars",
    "side_effects": [],
    "external_calls": [],
}


def reply(entries: list[dict[str, Any]]) -> str:
    return "Here you go:\n```json\n" + json.dumps({"entries": entries}) + "\n```"


def test_parse_accepts_fenced_or_bare_json_and_normalises_entries() -> None:
    [entry] = parse_metadata_response(reply([ENTRY]))
    assert entry["source_types"] == ["database", "cache"]  # unknown types dropped
    assert entry["transformations"] == ["cents to dollars"]  # string -> list
    assert entry["notes"] == ""
    bare = parse_metadata_response(json.dumps({"entries": [{**ENTRY, "kind": "rpc"}]}))
    assert bare[0]["kind"] == "other"
    assert parse_metadata_response('{"entries": [{"kind": "endpoint"}]}') == []  # no name


@pytest.mark.parametrize(
    ("text", "message"),
    [("no json here", "JSON object"), ("{broken", "JSON object"), ('{"x": 1}', "entries")],
)
def test_parse_rejects_unusable_replies(text: str, message: str) -> None:
    with pytest.raises(MetadataGenerationError, match=message):
        parse_metadata_response(text)


def test_only_non_test_code_is_sent_and_the_budget_is_respected() -> None:
    files = [
        SourceFile("r", "app/main.py", "python", "print(1)\n"),
        SourceFile("r", "tests/test_main.py", "python", "assert True\n"),
        SourceFile("r", "README.md", "markdown", "# hi\n"),
        SourceFile("r", "app/big.py", "python", "x = 1\n" * 500),
    ]
    text, paths, truncated = select_source_files(files, max_chars=200)
    assert paths == ["app/main.py"]
    assert truncated
    assert "=== app/main.py ===\n   1  print(1)" in text


def index_of(name: str) -> RepoIndex:
    return {"name": name, "owner": "local", "commit": "abc"}


def test_generate_makes_one_call_and_records_usage() -> None:
    seen: list[list[BaseMessage]] = []

    def respond(messages: list[BaseMessage]) -> AIMessage:
        seen.append(messages)
        return AIMessage(content=reply([ENTRY]), usage_metadata=usage(900, 120))

    from repo_analyzer.agent.usage import UsageTracker

    tracker = UsageTracker()
    files = [SourceFile("shop", "app/routers/customers.py", "python", "def f():\n    pass\n")]
    metadata = generate_endpoint_metadata(
        index_of("shop"), files, FunctionChatModel(respond=respond), usage=tracker
    )

    assert len(seen) == 1
    prompt = str(seen[0][-1].content)
    assert isinstance(seen[0][-1], HumanMessage)
    assert "Repository: shop" in prompt and "app/routers/customers.py" in prompt
    assert metadata["repository"] == "shop" and metadata["commit"] == "abc"
    assert metadata["files_read"] == ["app/routers/customers.py"]
    assert metadata["entries"][0]["name"] == "GET /customers/{id}/summary"
    assert tracker.to_dict()["total_tokens"] == 1020


def test_store_round_trips_next_to_the_index(tmp_path: Path) -> None:
    store = MetadataStore(tmp_path)
    metadata = {"schema_version": 1, "repository": "shop", "owner": "local", "entries": []}
    path = store.save(metadata)
    assert path.name == metadata_filename("local", "shop") == "local__shop.metadata.json"
    assert store.load("local", "shop") == metadata
    store.delete("local", "shop")
    assert store.load("local", "shop") is None


def make_indexing(
    settings: Settings, hook: Any, store: MetadataStore
) -> tuple[IndexingService, Workspace]:
    workspace = Workspace(settings.workspace_file)
    fetcher = RepoFetcher(
        settings.checkouts_dir, allowed_hosts=settings.allowed_git_hosts, allow_local_paths=True
    )
    service = IndexingService(
        settings,
        fetcher,
        RepoIndexStore(settings.repos_dir),
        workspace,
        metadata_hook=hook,
        metadata_store=store,
    )
    return service, workspace


def test_indexing_writes_metadata_and_the_agent_tool_reads_it(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    store = MetadataStore(settings.repos_dir)

    def hook(index: RepoIndex, root: Path) -> dict[str, Any]:
        assert root.is_dir()
        return {
            "schema_version": 1,
            "repository": index["name"],
            "owner": index["owner"],
            "entries": [ENTRY],
        }

    service, workspace = make_indexing(settings, hook, store)
    service.run([str(fixture_repos["mid-lib"])])
    assert (settings.repos_dir / "local__mid-lib.metadata.json").is_file()

    tools = {
        t.name: t
        for t in build_tools(settings, RepoIndexStore(settings.repos_dir), workspace, None, store)
    }
    message = tools["get_endpoint_metadata"].invoke(
        {
            "type": "tool_call",
            "id": "1",
            "name": "get_endpoint_metadata",
            "args": {"repo": "mid-lib"},
        }
    )
    assert '"GET /customers/{id}/summary"' in str(message.content)
    assert message.artifact == [
        {"repo": "mid-lib", "path": "app/routers/customers.py", "start_line": 26, "end_line": 26}
    ]

    # Re-indexing without mid-lib removes its metadata with its index.
    service.run([str(fixture_repos["core-lib"])])
    assert not (settings.repos_dir / "local__mid-lib.metadata.json").exists()


def test_a_metadata_failure_does_not_fail_indexing(
    settings: Settings, fixture_repos: dict[str, Path]
) -> None:
    def broken(index: RepoIndex, root: Path) -> dict[str, Any]:
        raise MetadataGenerationError("model said no")

    service, _ = make_indexing(settings, broken, MetadataStore(settings.repos_dir))
    state = service.run([str(fixture_repos["core-lib"])])
    assert [r.name for r in state.repositories] == ["core-lib"]
    assert not list(settings.repos_dir.glob("*.metadata.json"))


def test_the_metadata_tool_is_absent_unless_enabled(
    settings: Settings, index_store: RepoIndexStore, workspace: Workspace
) -> None:
    names = [t.name for t in build_tools(settings, index_store, workspace)]
    assert "get_endpoint_metadata" not in names
