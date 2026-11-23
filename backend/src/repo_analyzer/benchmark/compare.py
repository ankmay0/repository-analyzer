"""Answer one free-form question under every approach at once, streaming each answer.

This reuses the benchmark's approach definitions (tools, prompts, metadata and embedding
setup); the only new part is running the approaches concurrently against one question and
tagging each event with the approach it came from.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from repo_analyzer.agent.github_tools import GitHubClient, GitHubSource
from repo_analyzer.benchmark.approaches import (
    APPROACH_SUMMARIES,
    APPROACHES,
    ApproachContext,
    ApproachUnavailableError,
    build_approach,
    build_chat_service,
)
from repo_analyzer.benchmark.service import BenchmarkService, github_sources
from repo_analyzer.benchmark.setup import (
    ApproachSetup,
    SetupCost,
    index_fixture_repositories,
)
from repo_analyzer.config import Settings
from repo_analyzer.errors import (
    BenchmarkUnavailableError,
    InvalidBenchmarkRequestError,
    LLMNotConfiguredError,
)
from repo_analyzer.ingestion.metadata import MetadataStore
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.retrieval.embeddings import EmbeddingIndex
from repo_analyzer.workspace import IndexedRepository, Workspace, WorkspaceState

logger = logging.getLogger(__name__)

FIXTURES_ID = "benchmark-fixtures"
FIXTURES_LABEL = "Benchmark fixtures (shop-api + shop-web)"
NOT_ON_GITHUB = "this repository is not hosted on GitHub"
_GITHUB_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_END = object()


@dataclass(slots=True)
class CompareRequest:
    question: str
    repository: str = FIXTURES_ID
    approaches: list[str] | None = None
    github_ref: str | None = None


@dataclass(slots=True)
class CompareTarget:
    """A set of repositories prepared for answering: index, workspace and cached setup."""

    key: str
    label: str
    settings: Settings
    index_store: RepoIndexStore
    workspace: Workspace
    setup: ApproachSetup
    github_sources: dict[str, GitHubSource] | None
    github_reason: str | None
    metadata_store: MetadataStore | None = None
    embedding_index: EmbeddingIndex | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def ensure_metadata(self) -> tuple[MetadataStore, SetupCost | None]:
        """Generate (or load) endpoint metadata once; the cost is reported only to the caller
        that paid for it."""
        with self.lock:
            if self.metadata_store is not None:
                return self.metadata_store, None
            self.metadata_store, cost = self.setup.generate_metadata(reuse=True)
            return self.metadata_store, cost

    def ensure_embeddings(self) -> tuple[EmbeddingIndex, SetupCost | None]:
        with self.lock:
            if self.embedding_index is not None:
                return self.embedding_index, None
            self.embedding_index, cost = self.setup.build_embeddings()
            return self.embedding_index, cost


def _github_source_for(repository: IndexedRepository) -> GitHubSource | None:
    match = _GITHUB_URL.match(repository.source.strip())
    if match is None:
        return None
    ref = repository.commit or repository.default_branch or "HEAD"
    return GitHubSource(match.group(1), match.group(2), ref, "")


def _token_usage(cost: SetupCost | None) -> dict[str, Any] | None:
    if cost is None:
        return None
    return {"description": cost.description, "total_tokens": int(cost.usage.get("total_tokens", 0))}


class CompareService:
    def __init__(self, benchmark: BenchmarkService, workspace: Workspace) -> None:
        self._benchmark = benchmark
        self._settings = benchmark.settings
        self._workspace = workspace
        self._targets: dict[str, CompareTarget] = {}
        self._targets_lock = threading.Lock()

    # --- what can run -------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        repositories: list[dict[str, str]] = []
        try:
            self._benchmark.fixture_repositories()
            repositories.append({"id": FIXTURES_ID, "label": FIXTURES_LABEL, "kind": "fixtures"})
        except BenchmarkUnavailableError:
            pass  # not running from a checkout: only indexed repositories are offered
        repositories += [
            {"id": r.name, "label": f"{r.owner}/{r.name}", "kind": "workspace"}
            for r in self._workspace.state.repositories
        ]
        return {
            "llm_configured": self._benchmark.llm_configured,
            "llm_message": None
            if self._benchmark.llm_configured
            else "ANTHROPIC_API_KEY is not set on the server, so questions cannot be answered",
            "llm_model": self._settings.llm_model,
            "repositories": repositories,
            "approaches": [
                {
                    "name": name,
                    "summary": APPROACH_SUMMARIES[name],
                    "available": (reason := self._benchmark.unavailable_reason(name)) is None,
                    "reason": reason,
                }
                for name in APPROACHES
            ],
        }

    # --- preparing a target -------------------------------------------------------

    def resolve(self, request: CompareRequest) -> CompareTarget:
        """Validate the request and prepare the repository (blocking: call from a thread).

        Raises ``LLMNotConfiguredError``, ``InvalidBenchmarkRequestError`` or
        ``UnknownRepositoryError`` so the caller can answer with a status code before any
        event is streamed.
        """
        if not self._benchmark.llm_configured:
            raise LLMNotConfiguredError(
                "ANTHROPIC_API_KEY is not set on the server, so questions cannot be answered"
            )
        for name in request.approaches or []:
            if name not in APPROACHES:
                raise InvalidBenchmarkRequestError(
                    f"Unknown approach {name!r}; choose from {', '.join(APPROACHES)}"
                )
        if request.repository == FIXTURES_ID:
            return self._fixtures_target(request.github_ref)
        return self._workspace_target(request.repository)

    def _fixtures_target(self, github_ref: str | None) -> CompareTarget:
        names = self._benchmark.fixture_repositories()
        ref = github_ref or self._settings.benchmark_github_ref
        key = f"fixtures@{ref}"
        with self._targets_lock:
            target = self._targets.get(key)
            if target is not None:
                return target
            settings = self._settings.model_copy(
                update={
                    "data_dir": self._settings.data_dir / "compare" / "fixtures",
                    "allow_local_paths": True,
                }
            )
            index_store = RepoIndexStore(settings.repos_dir)
            workspace = Workspace(settings.workspace_file)
            index_fixture_repositories(
                settings, index_store, workspace, settings.benchmarks_dir / "fixtures", names
            )
            has_token = self._benchmark.unavailable_reason("json-github") is None
            target = self._new_target(
                key,
                FIXTURES_LABEL,
                settings,
                index_store,
                workspace,
                github_sources(
                    self._settings.benchmark_github_repo,
                    ref,
                    self._settings.benchmark_github_root,
                    names,
                )
                if has_token
                else None,
                None,
            )
            self._targets[key] = target
            return target

    def _workspace_target(self, name: str) -> CompareTarget:
        repository = self._workspace.state.repository(name)  # UnknownRepositoryError -> 404
        key = f"{repository.name}@{repository.commit or repository.index_file}"
        with self._targets_lock:
            target = self._targets.get(key)
            if target is not None:
                return target
            scoped = self._settings.data_dir / "compare" / "workspaces" / key.replace("/", "_")
            workspace = Workspace(scoped / "workspace.json")
            workspace.replace(WorkspaceState((repository,), self._workspace.state.indexed_at))
            source = _github_source_for(repository)
            target = self._new_target(
                key,
                f"{repository.owner}/{repository.name}",
                self._settings,
                RepoIndexStore(self._settings.repos_dir),
                workspace,
                {repository.name: source} if source is not None else None,
                NOT_ON_GITHUB if source is None else None,
            )
            self._targets[key] = target
            return target

    def _new_target(
        self,
        key: str,
        label: str,
        settings: Settings,
        index_store: RepoIndexStore,
        workspace: Workspace,
        sources: dict[str, GitHubSource] | None,
        github_reason: str | None,
    ) -> CompareTarget:
        return CompareTarget(
            key=key,
            label=label,
            settings=settings,
            index_store=index_store,
            workspace=workspace,
            setup=ApproachSetup(
                settings,
                index_store,
                workspace,
                self._benchmark.make_model,
                self._benchmark.embedder_factory(),
            ),
            github_sources=sources,
            github_reason=github_reason,
        )

    # --- streaming ----------------------------------------------------------------

    def availability(self, target: CompareTarget, names: Sequence[str]) -> list[dict[str, Any]]:
        rows = []
        for name in names:
            reason = self._benchmark.unavailable_reason(name)
            if reason is None and name == "json-github":
                reason = target.github_reason
            rows.append(
                {
                    "name": name,
                    "summary": APPROACH_SUMMARIES[name],
                    "available": reason is None,
                    "reason": reason,
                }
            )
        return rows

    async def events(
        self, target: CompareTarget, request: CompareRequest
    ) -> AsyncIterator[dict[str, Any]]:
        names = list(dict.fromkeys(request.approaches or APPROACHES))
        plan = self.availability(target, names)
        yield {
            "type": "plan",
            "question": request.question,
            "repository": request.repository,
            "label": target.label,
            "approaches": plan,
        }
        runnable = [row["name"] for row in plan if row["available"]]
        if not runnable:
            yield {"type": "done"}
            return

        client = self._benchmark.github_client() if "json-github" in runnable else None
        queue: asyncio.Queue[Any] = asyncio.Queue()
        run_id = secrets.token_hex(4)
        tasks = [
            asyncio.create_task(self._run_approach(name, target, request, client, run_id, queue))
            for name in runnable
        ]
        finished = 0
        try:
            while finished < len(tasks):
                event = await queue.get()
                if event is _END:
                    finished += 1
                else:
                    yield event
            yield {"type": "done"}
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if client is not None:
                client.close()

    async def _run_approach(
        self,
        name: str,
        target: CompareTarget,
        request: CompareRequest,
        client: GitHubClient | None,
        run_id: str,
        queue: asyncio.Queue[Any],
    ) -> None:
        def emit(kind: str, **data: Any) -> None:
            queue.put_nowait({"type": kind, "approach": name, **data})

        try:
            context = ApproachContext(
                target.settings,
                target.index_store,
                target.workspace,
                github_client=client,
                github_sources=target.github_sources,
            )
            cost: SetupCost | None = None
            if name == "json-metadata":
                emit("setup", message="Preparing endpoint metadata")
                context.metadata_store, cost = await asyncio.to_thread(target.ensure_metadata)
            elif name == "json-embeddings":
                emit("setup", message="Preparing code embeddings")
                context.embedding_index, cost = await asyncio.to_thread(target.ensure_embeddings)
            approach = build_approach(name, context)
            chat = build_chat_service(
                approach,
                target.settings,
                self._benchmark.make_model,
                prompt_caching=self._benchmark.prompt_caching,
            )
            emit("started")
            async for event in chat.stream(
                f"compare-{run_id}-{name}", approach.prepare_message(request.question)
            ):
                if event.type == "token":
                    emit("token", text=event.data.get("text", ""))
                elif event.type == "tool_call":
                    emit("tool_call", name=event.data.get("name"), args=event.data.get("args", {}))
                elif event.type == "error":
                    emit("error", message=event.data.get("message", "Unknown error"))
                elif event.type == "usage":
                    emit("usage", usage=event.data, setup=_token_usage(cost))
        except ApproachUnavailableError as exc:
            emit("unavailable", reason=str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Compare: %s failed", name)
            emit("error", message=f"{type(exc).__name__}: {exc}")
        finally:
            emit("approach_done")
            queue.put_nowait(_END)
