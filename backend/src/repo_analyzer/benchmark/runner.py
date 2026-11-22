"""Run every question under every requested approach and collect measurements."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.github_tools import GitHubClient, GitHubSource
from repo_analyzer.agent.service import ChatService
from repo_analyzer.agent.usage import UsageTracker
from repo_analyzer.benchmark.approaches import (
    APPROACHES,
    ApproachContext,
    ApproachUnavailableError,
    build_approach,
)
from repo_analyzer.benchmark.grading import grade_key_facts, judge_answer
from repo_analyzer.benchmark.questions import Question
from repo_analyzer.config import Settings
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.metadata import MetadataStore, generate_endpoint_metadata
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.retrieval.embeddings import Embedder, EmbeddingIndex, chunk_files
from repo_analyzer.workspace import IndexedRepository, Workspace

logger = logging.getLogger(__name__)

ModelFactory = Callable[[], BaseChatModel]
ProgressCallback = Callable[[dict[str, Any]], None]


@dataclass(slots=True)
class QuestionResult:
    approach: str
    question_id: str
    category: str
    answer: str
    facts_matched: int
    facts_total: int
    missing_facts: list[str]
    correct: bool
    judge_correct: bool | None
    judge_reason: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    llm_calls: int
    tool_calls: int
    latency_ms: int
    tools_used: list[str]
    error: str
    matched_facts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SetupCost:
    """One-off work an approach needs before questions (metadata, embeddings)."""

    approach: str
    description: str
    usage: dict[str, int]


@dataclass(slots=True)
class BenchmarkResult:
    results: list[QuestionResult] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    setup: list[SetupCost] = field(default_factory=list)
    judge_usage: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class BenchmarkConfig:
    settings: Settings
    fixtures_dir: Path
    repositories: Sequence[str]
    questions: Sequence[Question]
    work_dir: Path
    approaches: Sequence[str] = APPROACHES
    judge: bool = False
    top_k: int = 8
    prompt_caching: bool = True
    github_client: GitHubClient | None = None
    github_sources: dict[str, GitHubSource] | None = None
    embedder_factory: Callable[[], Embedder] | None = None
    # Called with {"type": ..., ...} as the run advances: approach_setup, approach_started,
    # approach_skipped, question_started and result. Used by the web UI for live progress.
    on_progress: ProgressCallback | None = None


class BenchmarkRunner:
    def __init__(self, config: BenchmarkConfig, model_factory: ModelFactory) -> None:
        self._config = config
        self._model_factory = model_factory
        self._settings = config.settings.model_copy(
            update={"data_dir": config.work_dir / "data", "allow_local_paths": True}
        )
        self._index_store = RepoIndexStore(self._settings.repos_dir)
        self._workspace = Workspace(self._settings.workspace_file)

    def _emit(self, event: dict[str, Any]) -> None:
        if self._config.on_progress is not None:
            self._config.on_progress(event)

    def index_fixtures(self) -> tuple[IndexedRepository, ...]:
        """Index the fixture repositories with Ctags into the work directory."""
        fetcher = RepoFetcher(
            self._settings.checkouts_dir,
            allowed_hosts=self._settings.allowed_git_hosts,
            allow_local_paths=True,
        )
        service = IndexingService(self._settings, fetcher, self._index_store, self._workspace)
        sources = [str(self._config.fixtures_dir / name) for name in self._config.repositories]
        return service.run(sources).repositories

    def _source_files(self, name: str) -> list[Any]:
        repository = self._workspace.state.repository(name)
        return list(
            scan_repository(
                name, Path(repository.root), max_file_bytes=self._settings.max_file_bytes
            )
        )

    def _generate_metadata(self, result: BenchmarkResult) -> MetadataStore:
        store = MetadataStore(self._settings.repos_dir)
        usage = UsageTracker()
        model = self._model_factory()
        for repository in self._workspace.state.repositories:
            index = self._index_store.load(repository.index_file)
            metadata = generate_endpoint_metadata(
                index, self._source_files(repository.name), model, usage=usage
            )
            store.save(metadata)
        usage.finish()
        result.setup.append(
            SetupCost(
                "json-metadata", "endpoint metadata, one model call per repository", usage.to_dict()
            )
        )
        return store

    def _build_embeddings(self, result: BenchmarkResult) -> EmbeddingIndex:
        if self._config.embedder_factory is None:
            raise ApproachUnavailableError("no embedder configured")
        usage = UsageTracker()
        embedder = self._config.embedder_factory()
        chunks = [
            chunk
            for repository in self._workspace.state.repositories
            for chunk in chunk_files(self._source_files(repository.name))
        ]
        index = EmbeddingIndex.build(embedder, chunks)
        index.save(self._config.work_dir / "embeddings.json")
        usage.finish()
        result.setup.append(
            SetupCost(
                "json-embeddings",
                f"embedded {len(chunks)} chunks with {embedder.name} (local, no LLM tokens)",
                usage.to_dict(),
            )
        )
        return index

    async def run(self) -> BenchmarkResult:
        result = BenchmarkResult()
        self.index_fixtures()
        context = ApproachContext(
            self._settings,
            self._index_store,
            self._workspace,
            github_client=self._config.github_client,
            github_sources=self._config.github_sources,
            top_k=self._config.top_k,
        )
        judge_usage = UsageTracker()

        for name in self._config.approaches:
            try:
                if name in ("json-metadata", "json-embeddings"):
                    self._emit({"type": "approach_setup", "approach": name})
                if name == "json-metadata":
                    context.metadata_store = self._generate_metadata(result)
                if name == "json-embeddings":
                    context.embedding_index = self._build_embeddings(result)
                approach = build_approach(name, context)
            except ApproachUnavailableError as exc:
                logger.warning("Skipping %s: %s", name, exc)
                result.skipped[name] = str(exc)
                self._emit({"type": "approach_skipped", "approach": name, "reason": str(exc)})
                continue
            self._emit({"type": "approach_started", "approach": name})

            chat = ChatService(
                self._model_factory,
                approach.tools,
                recursion_limit=self._settings.agent_recursion_limit,
                prompt_caching=self._config.prompt_caching,
                system_prompt=approach.system_prompt,
            )
            for question in self._config.questions:
                logger.info("[%s] %s", name, question.id)
                self._emit(
                    {"type": "question_started", "approach": name, "question_id": question.id}
                )
                row = await self._ask(chat, name, question, approach.prepare_message)
                if self._config.judge and row.answer:
                    verdict = judge_answer(
                        self._model_factory(),
                        question.question,
                        question.expected_answer,
                        row.answer,
                        usage=judge_usage,
                    )
                    row.judge_correct, row.judge_reason = verdict.correct, verdict.reason
                result.results.append(row)
                self._emit({"type": "result", "result": row})

        if self._config.judge:
            judge_usage.finish()
            result.judge_usage = judge_usage.to_dict()
        if self._config.github_client is not None:
            self._config.github_client.close()
        return result

    async def _ask(
        self,
        chat: ChatService,
        approach: str,
        question: Question,
        prepare: Callable[[str], str],
    ) -> QuestionResult:
        answer: list[str] = []
        tools: list[str] = []
        errors: list[str] = []
        usage: dict[str, Any] = {}
        async for event in chat.stream(f"{approach}:{question.id}", prepare(question.question)):
            if event.type == "token":
                answer.append(str(event.data.get("text", "")))
            elif event.type == "tool_call":
                tools.append(str(event.data.get("name")))
            elif event.type == "error":
                errors.append(str(event.data.get("message")))
            elif event.type == "usage":
                usage = dict(event.data)
        text = "".join(answer).strip()
        grade = grade_key_facts(text, question.key_facts)
        return QuestionResult(
            approach=approach,
            question_id=question.id,
            category=question.category,
            answer=text,
            facts_matched=len(grade.matched),
            facts_total=grade.total,
            missing_facts=list(grade.missing),
            correct=grade.correct,
            judge_correct=None,
            judge_reason="",
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            total_tokens=int(usage.get("total_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_tokens", 0)),
            llm_calls=int(usage.get("llm_calls", 0)),
            tool_calls=int(usage.get("tool_calls", 0)),
            latency_ms=int(usage.get("latency_ms", 0)),
            tools_used=tools,
            error="; ".join(errors),
            matched_facts=list(grade.matched),
        )
