"""Shared fixtures. See ``support.py`` for the fixture repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.embeddings import HashingEmbedder
from repo_analyzer.retrieval.store import VectorStore
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
        knowledge_mode="rag",
        embedding_provider="hashing",
        chunk_max_lines=20,
        chunk_overlap_lines=2,
    )


@pytest.fixture
def store(settings: Settings) -> VectorStore:
    return VectorStore(settings.index_dir, HashingEmbedder())


@pytest.fixture
def workspace(settings: Settings) -> Workspace:
    return Workspace(settings.workspace_file)


@pytest.fixture
def knowledge(settings: Settings) -> KnowledgeStore:
    return KnowledgeStore(settings.knowledge_dir)


def make_indexing(
    settings: Settings, workspace: Workspace, knowledge: KnowledgeStore, store: VectorStore | None
) -> IndexingService:
    fetcher = RepoFetcher(
        settings.repos_dir, allowed_hosts=settings.allowed_git_hosts, allow_local_paths=True
    )
    return IndexingService(settings, fetcher, workspace, knowledge, store)


@pytest.fixture
def indexing(
    settings: Settings, store: VectorStore, workspace: Workspace, knowledge: KnowledgeStore
) -> IndexingService:
    """Indexing in rag mode (vector store)."""
    return make_indexing(settings, workspace, knowledge, store)


@pytest.fixture
def indexed_workspace(
    indexing: IndexingService, fixture_repos: dict[str, Path], workspace: Workspace
) -> Workspace:
    indexing.run([str(path) for path in fixture_repos.values()])
    return workspace


# --- json mode: the same repositories indexed into pack, map and graph files ---


@pytest.fixture
def json_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"knowledge_mode": "json"})


@pytest.fixture
def json_workspace(
    json_settings: Settings,
    workspace: Workspace,
    knowledge: KnowledgeStore,
    fixture_repos: dict[str, Path],
) -> Workspace:
    make_indexing(json_settings, workspace, knowledge, None).run(
        [str(path) for path in fixture_repos.values()]
    )
    return workspace
