"""Shared fixtures. See ``support.py`` for the fixture repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.workspace import Workspace
from support import FIXTURE_REPOS, write_tree


@pytest.fixture
def fixture_repos(tmp_path: Path) -> dict[str, Path]:
    return {
        name: write_tree(tmp_path / "sources" / name, files)
        for name, files in FIXTURE_REPOS.items()
    }


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        allow_local_paths=True,
    )


@pytest.fixture
def index_store(settings: Settings) -> RepoIndexStore:
    return RepoIndexStore(settings.repos_dir)


@pytest.fixture
def workspace(settings: Settings) -> Workspace:
    return Workspace(settings.workspace_file)


@pytest.fixture
def indexing(
    settings: Settings, index_store: RepoIndexStore, workspace: Workspace
) -> IndexingService:
    fetcher = RepoFetcher(
        settings.checkouts_dir, allowed_hosts=settings.allowed_git_hosts, allow_local_paths=True
    )
    return IndexingService(settings, fetcher, index_store, workspace)


@pytest.fixture
def indexed_workspace(
    indexing: IndexingService, fixture_repos: dict[str, Path], workspace: Workspace
) -> Workspace:
    indexing.run([str(path) for path in fixture_repos.values()])
    return workspace
