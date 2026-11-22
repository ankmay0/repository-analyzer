from __future__ import annotations

import json
from pathlib import Path

import pytest

from repo_analyzer.domain import RepoSnapshot
from repo_analyzer.errors import InvalidRepositorySourceError, RepositoryFetchError
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.workspace import Workspace


def test_indexing_writes_one_json_file_per_repository(
    indexing: IndexingService, fixture_repos: dict[str, Path], index_store: RepoIndexStore
) -> None:
    progress: list[str] = []

    state = indexing.run([str(p) for p in fixture_repos.values()], progress.append)

    assert [r.name for r in state.repositories] == ["core-lib", "mid-lib", "web-app", "tools"]
    assert all(r.file_count > 0 for r in state.repositories)
    assert {r.index_file for r in state.repositories} == {
        "local__core-lib.json",
        "local__mid-lib.json",
        "local__web-app.json",
        "local__tools.json",
    }
    for repository in state.repositories:
        index = index_store.load(repository.index_file)
        assert index["name"] == repository.name
        assert index["stats"]["files"] == repository.file_count == len(index["files"])
    assert progress[-1] == "Indexed 4 repositories"


def test_repository_json_describes_files_symbols_and_dependencies(
    indexing: IndexingService, fixture_repos: dict[str, Path], index_store: RepoIndexStore
) -> None:
    indexing.run([str(p) for p in fixture_repos.values()])

    mid = index_store.load("local__mid-lib.json")

    assert (mid["schema_version"], mid["owner"], mid["name"]) == (1, "local", "mid-lib")
    assert mid["url"] == str(fixture_repos["mid-lib"].resolve())
    assert mid["indexed_at"].endswith("+00:00")
    assert mid["manifests"] == [
        {"path": "pyproject.toml", "ecosystem": "pypi", "package": "mid-lib"}
    ]
    assert mid["dependencies"] == [
        {"name": "core-lib", "ecosystem": "pypi", "kind": "runtime", "manifest": "pyproject.toml"}
    ]  # "Core.Lib" in the manifest is normalised to core-lib
    assert mid["stats"]["languages"] == {"python": 1, "toml": 1}

    session = next(f for f in mid["files"] if f["path"] == "src/mid_lib/session.py")
    source = (fixture_repos["mid-lib"] / "src/mid_lib/session.py").read_text()
    assert session["language"] == "python"
    assert session["size_bytes"] == len(source.encode())
    assert session["line_count"] == 6
    assert session["symbols"] == {
        "functions": [{"name": "create_session", "line": 4, "end_line": 6}],
        "classes": [],
        "imports": [{"module": "core_lib.tokens", "line": 1}],
    }

    web = index_store.load("local__web-app.json")
    assert {(d["name"], d["kind"]) for d in web["dependencies"]} == {
        ("mid-lib", "runtime"),
        ("core-lib", "dev"),
    }
    tools = index_store.load("local__tools.json")
    assert {(d["name"], d["ecosystem"], d["kind"]) for d in tools["dependencies"]} == {
        ("left-pad", "npm", "dev"),
        ("web-app", "pypi", "dev"),
    }


def test_secrets_never_reach_the_index_and_no_file_contents_are_stored(
    indexing: IndexingService, fixture_repos: dict[str, Path], index_store: RepoIndexStore
) -> None:
    indexing.run([str(p) for p in fixture_repos.values()])

    core_path = index_store.path_for("local__core-lib.json")
    raw = core_path.read_text(encoding="utf-8")
    index = json.loads(raw)

    assert [f["path"] for f in index["files"]] == ["pyproject.toml", "src/core_lib/tokens.py"]
    assert ".env" not in raw and "do-not-index" not in raw
    assert "Sign a payload" not in raw  # file contents (here, a docstring) are not copied


def test_symlinks_are_not_indexed(
    indexing: IndexingService,
    fixture_repos: dict[str, Path],
    index_store: RepoIndexStore,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def leaked(): ...\n")
    (fixture_repos["core-lib"] / "link.py").symlink_to(outside)

    indexing.run([str(fixture_repos["core-lib"])])

    paths = [f["path"] for f in index_store.load("local__core-lib.json")["files"]]
    assert "link.py" not in paths


def test_state_is_persisted_and_reloaded(indexed_workspace: Workspace) -> None:
    reloaded = Workspace(indexed_workspace._state_file).state

    assert reloaded.repositories == indexed_workspace.state.repositories
    assert reloaded.indexed_at == indexed_workspace.state.indexed_at
    assert reloaded.repositories[0].symbol_count == 1  # core-lib: sign_token


def test_reindexing_with_fewer_repositories_deletes_the_removed_json_files(
    indexing: IndexingService, fixture_repos: dict[str, Path], index_store: RepoIndexStore
) -> None:
    indexing.run([str(p) for p in fixture_repos.values()])

    state = indexing.run([str(fixture_repos["core-lib"]), str(fixture_repos["mid-lib"])])

    assert [r.name for r in state.repositories] == ["core-lib", "mid-lib"]
    remaining = sorted(p.name for p in index_store.path_for("x").parent.glob("*.json"))
    assert remaining == ["local__core-lib.json", "local__mid-lib.json"]


def test_a_failed_run_leaves_previous_indexes_untouched(
    indexing: IndexingService,
    fixture_repos: dict[str, Path],
    index_store: RepoIndexStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    indexing.run([str(fixture_repos["core-lib"])])
    before = index_store.path_for("local__core-lib.json").read_text()
    real_fetch = indexing._fetcher.fetch

    def flaky_fetch(source: str) -> RepoSnapshot:
        if source.endswith("web-app"):
            raise RepositoryFetchError("simulated clone failure")
        return real_fetch(source)

    monkeypatch.setattr(indexing._fetcher, "fetch", flaky_fetch)

    with pytest.raises(RepositoryFetchError, match="simulated"):
        indexing.run([str(fixture_repos["mid-lib"]), str(fixture_repos["web-app"])])

    assert index_store.path_for("local__core-lib.json").read_text() == before
    assert not index_store.path_for("local__mid-lib.json").exists()
    assert [r.name for r in indexing._workspace.state.repositories] == ["core-lib"]


def test_rejects_empty_duplicate_and_oversized_requests(
    indexing: IndexingService, fixture_repos: dict[str, Path]
) -> None:
    core = str(fixture_repos["core-lib"])
    with pytest.raises(InvalidRepositorySourceError, match="at least one"):
        indexing.run([])
    with pytest.raises(InvalidRepositorySourceError, match="duplicated: core-lib"):
        indexing.run([core, core])
    with pytest.raises(InvalidRepositorySourceError, match="At most"):
        indexing.run([core] * 11)


def test_corrupt_workspace_file_is_treated_as_empty(tmp_path: Path) -> None:
    state_file = tmp_path / "workspace.json"
    state_file.write_text("{ not json")
    assert Workspace(state_file).state.repositories == ()


def test_a_workspace_file_from_the_old_format_is_ignored(tmp_path: Path) -> None:
    state_file = tmp_path / "workspace.json"
    state_file.write_text(json.dumps({"schema_version": 1, "repositories": [], "graph": {}}))
    assert Workspace(state_file).state.repositories == ()
