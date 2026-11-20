"""Construction of the application's long-lived objects (the composition root)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.agent.service import ChatService
from repo_analyzer.agent.tools import build_tools
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.jobs import IndexJobRunner
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.retrieval.embeddings import Embedder, build_embedder
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    embedder: Embedder
    workspace: Workspace
    jobs: IndexJobRunner
    chat: ChatService


def build_services(
    settings: Settings,
    *,
    model_factory: Callable[[], BaseChatModel] | None = None,
) -> Services:
    embedder = build_embedder(settings)
    store = VectorStore(settings.index_dir, embedder)
    workspace = Workspace(settings.workspace_file)
    fetcher = RepoFetcher(
        settings.repos_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=settings.allow_local_paths,
        clone_timeout_seconds=settings.git_clone_timeout_seconds,
    )
    indexing = IndexingService(settings, fetcher, store, workspace)
    chat = ChatService(
        model_factory or (lambda: build_chat_model(settings)),
        build_tools(settings, store, workspace),
        recursion_limit=settings.agent_recursion_limit,
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
