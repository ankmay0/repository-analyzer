from __future__ import annotations

from pathlib import Path

import pytest

from repo_analyzer.domain import DependencyScope
from repo_analyzer.errors import InvalidRepositorySourceError
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace


def test_indexes_repositories_and_builds_the_dependency_graph(
    indexing: IndexingService, fixture_repos: dict[str, Path], store: VectorStore
) -> None:
    progress: list[str] = []

    state = indexing.run([str(p) for p in fixture_repos.values()], progress.append)

    assert [r.name for r in state.repositories] == ["core-lib", "mid-lib", "web-app", "tools"]
    assert all(r.file_count > 0 and r.chunk_count > 0 for r in state.repositories)
    assert store.count() == sum(r.chunk_count for r in state.repositories)
    assert {(e.source_repo, e.target_repo, e.scope) for e in state.graph.edges} == {
        ("mid-lib", "core-lib", DependencyScope.RUNTIME),  # "Core.Lib" normalises to core-lib
        ("web-app", "mid-lib", DependencyScope.RUNTIME),
        ("web-app", "core-lib", DependencyScope.DEV),
        ("tools", "web-app", DependencyScope.DEV),
    }
    assert {i.repo: i.weak for i in state.graph.impact_of("core-lib")} == {
        "mid-lib": False,
        "web-app": False,
        "tools": True,
    }
    assert progress[-1] == "Indexed 4 repositories"


def test_secret_files_never_reach_the_index(
    indexed_workspace: Workspace, store: VectorStore
) -> None:
    hits = store.search("SECRET_KEY do-not-index", top_k=50)
    assert hits
    assert all(".env" not in h.chunk.path and "do-not-index" not in h.chunk.text for h in hits)


def test_state_is_persisted_and_reloaded(indexed_workspace: Workspace) -> None:
    reloaded = Workspace(indexed_workspace._state_file).state

    assert reloaded.repositories == indexed_workspace.state.repositories
    assert reloaded.graph.edges == indexed_workspace.state.graph.edges
    assert reloaded.indexed_at == indexed_workspace.state.indexed_at


def test_reindexing_with_fewer_repositories_drops_the_removed_ones(
    indexing: IndexingService, fixture_repos: dict[str, Path], store: VectorStore
) -> None:
    indexing.run([str(p) for p in fixture_repos.values()])

    state = indexing.run([str(fixture_repos["core-lib"]), str(fixture_repos["mid-lib"])])

    assert [r.name for r in state.repositories] == ["core-lib", "mid-lib"]
    assert {h.chunk.repo for h in store.search("session token", top_k=50)} == {
        "core-lib",
        "mid-lib",
    }
    assert store.count() == sum(r.chunk_count for r in state.repositories)


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
