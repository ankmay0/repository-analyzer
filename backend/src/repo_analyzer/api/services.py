"""Construction of the application's long-lived objects (the composition root)."""

from __future__ import annotations

from dataclasses import dataclass

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.agent.service import ChatService, ModelFactory
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.jobs import IndexJobRunner
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.embeddings import Embedder, build_embedder
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    embedder: Embedder | None  # None in json mode, which uses no embeddings
    workspace: Workspace
    jobs: IndexJobRunner
    chat: ChatService


def build_services(
    settings: Settings,
    *,
    model_factory: ModelFactory | None = None,
) -> Services:
    # The vector store and its embedding model exist only in rag mode; json mode
    # never loads them, so it needs no model download and no index directory.
    embedder: Embedder | None = None
    store: VectorStore | None = None
    if settings.knowledge_mode == "rag":
        embedder = build_embedder(settings)
        store = VectorStore(settings.index_dir, embedder)
    knowledge = KnowledgeStore(settings.knowledge_dir)
    workspace = Workspace(settings.workspace_file)
    fetcher = RepoFetcher(
        settings.repos_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=settings.allow_local_paths,
        clone_timeout_seconds=settings.git_clone_timeout_seconds,
    )
    indexing = IndexingService(settings, fetcher, workspace, knowledge, store)
    chat = ChatService(
        model_factory or (lambda role: build_chat_model(settings, role)),
        settings,
        workspace,
        knowledge,
        store,
        # Scripted test models do not accept provider-specific call options.
        prompt_caching=model_factory is None,
    )
    return Services(
        settings=settings,
        embedder=embedder,
        workspace=workspace,
        jobs=IndexJobRunner(indexing),
        chat=chat,
    )
