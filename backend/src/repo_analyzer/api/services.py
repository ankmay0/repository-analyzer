"""Construction of the application's long-lived objects (the composition root)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.agent.service import ChatService, build_chat_agent
from repo_analyzer.agent.skills import SkillLibrary
from repo_analyzer.benchmark.compare import CompareService
from repo_analyzer.benchmark.service import BenchmarkService
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.graph_store import GraphStore, Neo4jGraphStore, graph_workspace_key
from repo_analyzer.ingestion.jobs import IndexJobRunner
from repo_analyzer.ingestion.metadata import MetadataStore, generate_endpoint_metadata
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndex, RepoIndexStore
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.retrieval.embeddings import Embedder
from repo_analyzer.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    workspace: Workspace
    jobs: IndexJobRunner
    chat: ChatService
    skills: SkillLibrary
    benchmark: BenchmarkService
    compare: CompareService
    graph_store: GraphStore | None = None  # None unless RA_GRAPH_ENABLED
    graph_ws: str = ""


def _build_graph_store(settings: Settings, injected: GraphStore | None) -> GraphStore | None:
    """The Neo4j store when the graph is enabled; ``None`` keeps today's behaviour."""
    if not settings.graph_enabled:
        return None
    store = injected if injected is not None else Neo4jGraphStore.from_settings(settings)
    try:
        store.ensure_schema()
    except Exception as exc:  # Neo4j down or the extra missing: retried on first sync
        logger.warning("Code graph schema setup failed (will retry when indexing): %s", exc)
    return store


def build_services(
    settings: Settings,
    *,
    model_factory: Callable[[], BaseChatModel] | None = None,
    benchmark_embedder_factory: Callable[[], Embedder] | None = None,
    graph_store: GraphStore | None = None,
) -> Services:
    index_store = RepoIndexStore(settings.repos_dir)
    workspace = Workspace(settings.workspace_file)
    fetcher = RepoFetcher(
        settings.checkouts_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=settings.allow_local_paths,
        clone_timeout_seconds=settings.git_clone_timeout_seconds,
    )
    make_model = model_factory or (lambda: build_chat_model(settings))
    metadata_store = MetadataStore(settings.repos_dir) if settings.endpoint_metadata else None

    def describe_endpoints(index: RepoIndex, root: Path) -> dict[str, Any]:
        files = scan_repository(index["name"], root, max_file_bytes=settings.max_file_bytes)
        return generate_endpoint_metadata(index, files, make_model())

    graph = _build_graph_store(settings, graph_store)
    graph_ws = graph_workspace_key(settings.data_dir)
    indexing = IndexingService(
        settings,
        fetcher,
        index_store,
        workspace,
        metadata_hook=describe_endpoints if metadata_store is not None else None,
        metadata_store=metadata_store,
        graph_store=graph,
        graph_ws=graph_ws,
    )
    skills = SkillLibrary.load(settings.skills_dir)
    chat = build_chat_agent(
        settings,
        index_store,
        workspace,
        make_model,
        # Scripted test models do not accept provider-specific call options.
        prompt_caching=model_factory is None,
        skills=skills,
        metadata_store=metadata_store,
    )
    benchmark = BenchmarkService(
        settings,
        make_model,
        needs_api_key=model_factory is None,
        embedder_factory=benchmark_embedder_factory,
    )
    return Services(
        settings=settings,
        workspace=workspace,
        jobs=IndexJobRunner(indexing),
        chat=chat,
        skills=skills,
        benchmark=benchmark,
        compare=CompareService(benchmark, workspace),
        graph_store=graph,
        graph_ws=graph_ws,
    )
