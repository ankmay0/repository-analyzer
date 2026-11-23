"""Construction of the application's long-lived objects (the composition root)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.agent.service import ChatService
from repo_analyzer.agent.skills import SkillLibrary, build_system_prompt
from repo_analyzer.agent.tools import build_tools
from repo_analyzer.benchmark.compare import CompareService
from repo_analyzer.benchmark.service import BenchmarkService
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.jobs import IndexJobRunner
from repo_analyzer.ingestion.metadata import MetadataStore, generate_endpoint_metadata
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndex, RepoIndexStore
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.retrieval.embeddings import Embedder
from repo_analyzer.workspace import Workspace


@dataclass(frozen=True, slots=True)
class Services:
    settings: Settings
    workspace: Workspace
    jobs: IndexJobRunner
    chat: ChatService
    skills: SkillLibrary
    benchmark: BenchmarkService
    compare: CompareService


def build_services(
    settings: Settings,
    *,
    model_factory: Callable[[], BaseChatModel] | None = None,
    benchmark_github_transport: httpx.BaseTransport | None = None,
    benchmark_embedder_factory: Callable[[], Embedder] | None = None,
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

    indexing = IndexingService(
        settings,
        fetcher,
        index_store,
        workspace,
        metadata_hook=describe_endpoints if metadata_store is not None else None,
        metadata_store=metadata_store,
    )
    skills = SkillLibrary.load(settings.skills_dir)
    chat = ChatService(
        make_model,
        build_tools(settings, index_store, workspace, skills, metadata_store),
        recursion_limit=settings.agent_recursion_limit,
        # Scripted test models do not accept provider-specific call options.
        prompt_caching=model_factory is None,
        system_prompt=build_system_prompt(skills),
    )
    benchmark = BenchmarkService(
        settings,
        make_model,
        needs_api_key=model_factory is None,
        github_transport=benchmark_github_transport,
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
    )
