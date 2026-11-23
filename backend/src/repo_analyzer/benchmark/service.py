"""Run the benchmark inside the API server and expose its progress.

This is a thin layer over :class:`BenchmarkRunner`: it validates a request, runs the
runner on a worker thread (the runner makes blocking model calls), and records the
progress events the runner emits so the web UI can poll or stream them.
Runs are kept in memory; each finished run is also written to
``<data_dir>/benchmark/<run id>/`` in the same format as the command line tool.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.github_tools import GitHubClient, GitHubSource
from repo_analyzer.benchmark.approaches import APPROACH_SUMMARIES, APPROACHES
from repo_analyzer.benchmark.questions import Question, QuestionSet, load_questions
from repo_analyzer.benchmark.report import csv_text, summary_rows, write_report
from repo_analyzer.benchmark.runner import (
    BenchmarkConfig,
    BenchmarkResult,
    BenchmarkRunner,
    QuestionResult,
)
from repo_analyzer.benchmark.setup import SetupCost
from repo_analyzer.config import Settings
from repo_analyzer.errors import (
    AnalyzerError,
    BenchmarkRunInProgressError,
    BenchmarkUnavailableError,
    InvalidBenchmarkRequestError,
    LLMNotConfiguredError,
    UnknownBenchmarkRunError,
)
from repo_analyzer.retrieval.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    Embedder,
    FastEmbedEmbedder,
    fastembed_available,
)

logger = logging.getLogger(__name__)

MAX_KEPT_RUNS = 20
TERMINAL_EVENTS = ("finished", "failed")
GITHUB_TOKEN_MISSING = "GITHUB_TOKEN is not set on the server"  # noqa: S105 - a message
NO_FASTEMBED = 'fastembed is not installed (pip install -e ".[embeddings]")'


def github_sources(repo: str, ref: str, root: str, names: Sequence[str]) -> dict[str, GitHubSource]:
    """Where each fixture app lives on GitHub (``<root>/<name>`` at ``ref``)."""
    owner, _, name = repo.partition("/")
    return {n: GitHubSource(owner, name, ref, f"{root}/{n}") for n in names}


@dataclass(slots=True)
class BenchmarkRunRequest:
    approaches: list[str]
    question_ids: list[str] | None = None
    repositories: list[str] | None = None
    judge: bool = False
    github_ref: str | None = None


@dataclass(slots=True)
class BenchmarkRun:
    id: str
    approaches: list[str]
    question_ids: list[str]
    repositories: list[str]
    judge: bool
    github_ref: str
    total: int
    started_at: str
    state: str = "running"
    rows: list[QuestionResult] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    setup: list[SetupCost] = field(default_factory=list)
    judge_usage: dict[str, int] = field(default_factory=dict)
    current: dict[str, Any] | None = None
    error: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def done(self) -> bool:
        return self.state in ("finished", "failed")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            partial = BenchmarkResult(list(self.rows), dict(self.skipped), list(self.setup))
            return {
                "id": self.id,
                "state": self.state,
                "approaches": self.approaches,
                "question_ids": self.question_ids,
                "repositories": self.repositories,
                "judge": self.judge,
                "github_ref": self.github_ref,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "total": self.total,
                "completed": len(self.rows),
                "current": self.current,
                "rows": [asdict(r) for r in self.rows],
                "skipped": dict(self.skipped),
                "setup": [asdict(s) for s in self.setup],
                "judge_usage": dict(self.judge_usage),
                "summary": summary_rows(partial),
                "error": self.error,
                "next_event": len(self.events),
            }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class BenchmarkService:
    def __init__(
        self,
        settings: Settings,
        model_factory: Callable[[], BaseChatModel],
        *,
        needs_api_key: bool = True,
        github_transport: httpx.BaseTransport | None = None,
        embedder_factory: Callable[[], Embedder] | None = None,
    ) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._needs_api_key = needs_api_key
        self._github_transport = github_transport
        self._embedder_factory = embedder_factory
        self._runs: dict[str, BenchmarkRun] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- what can run -------------------------------------------------------------

    @property
    def llm_configured(self) -> bool:
        key = self._settings.anthropic_api_key
        return not self._needs_api_key or bool(key and key.get_secret_value())

    @property
    def _github_token(self) -> str | None:
        token = self._settings.github_token
        return token.get_secret_value() if token and token.get_secret_value() else None

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def prompt_caching(self) -> bool:
        """Scripted test models reject provider-specific cache options."""
        return self._needs_api_key

    def make_model(self) -> BaseChatModel:
        return self._model_factory()

    def github_client(self) -> GitHubClient | None:
        """A new client for the fixtures' GitHub repository, or None without a token."""
        token = self._github_token
        if token is None:
            return None
        return GitHubClient(
            token=token,
            max_file_bytes=self._settings.max_file_bytes,
            transport=self._github_transport,
        )

    def embedder_factory(self) -> Callable[[], Embedder] | None:
        """How to build an embedder, or None when the embeddings extra is missing."""
        return self._make_embedder if self.unavailable_reason("json-embeddings") is None else None

    def fixture_repositories(self) -> list[str]:
        """Fixture apps that exist on disk (from the questions file's repository list)."""
        return self._select_repositories(self.question_set(), None)

    def _make_embedder(self) -> Embedder:
        if self._embedder_factory is not None:
            return self._embedder_factory()
        return FastEmbedEmbedder(DEFAULT_EMBEDDING_MODEL)

    def unavailable_reason(self, approach: str) -> str | None:
        if approach == "json-github" and self._github_token is None:
            return GITHUB_TOKEN_MISSING
        if (
            approach == "json-embeddings"
            and self._embedder_factory is None
            and not fastembed_available()
        ):
            return NO_FASTEMBED
        return None

    def question_set(self) -> QuestionSet:
        path = self._settings.benchmarks_dir / "questions.yaml"
        if not path.is_file():
            raise BenchmarkUnavailableError(
                f"Benchmark questions not found at {path}. Run the server from a source checkout "
                "or set RA_BENCHMARKS_DIR."
            )
        return load_questions(path)

    def status(self) -> dict[str, Any]:
        fixtures = self._settings.benchmarks_dir / "fixtures"
        questions = self.question_set()
        return {
            "llm_configured": self.llm_configured,
            "llm_message": None
            if self.llm_configured
            else "ANTHROPIC_API_KEY is not set on the server, so a benchmark cannot run",
            "llm_model": self._settings.llm_model,
            "approaches": [
                {
                    "name": name,
                    "summary": APPROACH_SUMMARIES[name],
                    "available": (reason := self.unavailable_reason(name)) is None,
                    "reason": reason,
                }
                for name in APPROACHES
            ],
            "repositories": [r for r in questions.repositories if (fixtures / r).is_dir()],
            "question_count": len(questions.questions),
            "github_ref": self._settings.benchmark_github_ref,
            "running_run_id": next((r.id for r in self._runs.values() if not r.done), None),
        }

    # --- starting and reading runs ------------------------------------------------

    def start(self, request: BenchmarkRunRequest) -> BenchmarkRun:
        if not self.llm_configured:
            raise LLMNotConfiguredError(
                "ANTHROPIC_API_KEY is not set on the server, so a benchmark cannot run"
            )
        question_set = self.question_set()
        approaches = list(dict.fromkeys(request.approaches))
        if not approaches:
            raise InvalidBenchmarkRequestError("Choose at least one approach")
        for name in approaches:
            if name not in APPROACHES:
                raise InvalidBenchmarkRequestError(
                    f"Unknown approach {name!r}; choose from {', '.join(APPROACHES)}"
                )
            if (reason := self.unavailable_reason(name)) is not None:
                raise InvalidBenchmarkRequestError(f"{name} is unavailable: {reason}")

        questions = self._select_questions(question_set, request.question_ids)
        repositories = self._select_repositories(question_set, request.repositories)
        if running := next((r for r in self._runs.values() if not r.done), None):
            raise BenchmarkRunInProgressError(
                f"Benchmark run {running.id} is still running; wait for it to finish"
            )

        loop = asyncio.get_running_loop()
        self._loop = loop
        run = BenchmarkRun(
            id=f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            approaches=approaches,
            question_ids=[q.id for q in questions],
            repositories=repositories,
            judge=request.judge,
            github_ref=request.github_ref or self._settings.benchmark_github_ref,
            total=len(approaches) * len(questions),
            started_at=_now(),
            changed=asyncio.Event(),
        )
        self._runs[run.id] = run
        self._prune()
        self._publish(
            run,
            {
                "type": "plan",
                "approaches": approaches,
                "question_ids": run.question_ids,
                "total": run.total,
            },
        )
        task = loop.create_task(self._execute(run, questions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    def get(self, run_id: str) -> BenchmarkRun:
        try:
            return self._runs[run_id]
        except KeyError:
            raise UnknownBenchmarkRunError(f"No benchmark run {run_id!r}") from None

    def results_csv(self, run_id: str) -> str:
        run = self.get(run_id)
        with run.lock:
            return csv_text(list(run.rows))

    async def events(
        self, run_id: str, after: int = 0
    ) -> AsyncIterator[tuple[int, dict[str, Any]]]:
        """Yield ``(sequence number, event)`` from ``after`` on, until the run ends."""
        run = self.get(run_id)
        index = max(after, 0)
        while True:
            changed = run.changed  # grab before reading so a publish in between wakes us
            with run.lock:
                pending = run.events[index:]
            for event in pending:
                yield index, event
                index += 1
                if event["type"] in TERMINAL_EVENTS:
                    return
            if not pending:
                await changed.wait()

    # --- internals ----------------------------------------------------------------

    def _select_questions(self, question_set: QuestionSet, ids: list[str] | None) -> list[Question]:
        if not ids:
            return list(question_set.questions)
        known = {q.id for q in question_set.questions}
        if unknown := [i for i in ids if i not in known]:
            raise InvalidBenchmarkRequestError(f"Unknown question id(s): {', '.join(unknown)}")
        wanted = set(ids)
        return [q for q in question_set.questions if q.id in wanted]

    def _select_repositories(self, question_set: QuestionSet, names: list[str] | None) -> list[str]:
        fixtures = self._settings.benchmarks_dir / "fixtures"
        available = [r for r in question_set.repositories if (fixtures / r).is_dir()]
        if not names:
            if not available:
                raise BenchmarkUnavailableError(f"No fixture apps found in {fixtures}")
            return available
        if unknown := [n for n in names if n not in available]:
            raise InvalidBenchmarkRequestError(
                f"Unknown fixture repository: {', '.join(unknown)} (available: "
                f"{', '.join(available) or 'none'})"
            )
        return [r for r in available if r in set(names)]

    def _prune(self) -> None:
        finished = [r.id for r in self._runs.values() if r.done]
        for run_id in finished[: max(0, len(self._runs) - MAX_KEPT_RUNS)]:
            del self._runs[run_id]

    def _wake(self, run: BenchmarkRun) -> None:
        previous, run.changed = run.changed, asyncio.Event()
        previous.set()

    def _publish(self, run: BenchmarkRun, event: dict[str, Any]) -> None:
        """Record an event. Safe to call from the worker thread."""
        with run.lock:
            kind = event["type"]
            if kind in ("approach_setup", "approach_started", "question_started"):
                run.current = {
                    "approach": event["approach"],
                    "question_id": event.get("question_id"),
                    "phase": "setup" if kind == "approach_setup" else "asking",
                }
            elif kind == "approach_skipped":
                run.skipped[event["approach"]] = event["reason"]
                run.total -= len(run.question_ids)
                run.current = None
            elif kind == "result":
                run.rows.append(event["result"])
                event = {**event, "result": asdict(event["result"])}
            elif kind == "finished":
                run.state, run.current, run.finished_at = "finished", None, _now()
            elif kind == "failed":
                run.state, run.current, run.finished_at = "failed", None, _now()
                run.error = event["error"]
            event = {**event, "progress": {"completed": len(run.rows), "total": run.total}}
            run.events.append(event)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._wake, run)

    async def _execute(self, run: BenchmarkRun, questions: list[Question]) -> None:
        try:
            result = await asyncio.to_thread(self._run_blocking, run, questions)
        except AnalyzerError as exc:
            self._publish(run, {"type": "failed", "error": str(exc)})
        except Exception as exc:
            logger.exception("Benchmark run %s failed", run.id)
            self._publish(run, {"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
        else:
            with run.lock:
                run.setup = result.setup
                run.judge_usage = result.judge_usage
            self._save_report(run, result)
            self._publish(run, {"type": "finished"})

    def _run_blocking(self, run: BenchmarkRun, questions: list[Question]) -> BenchmarkResult:
        token = self._github_token
        config = BenchmarkConfig(
            settings=self._settings,
            fixtures_dir=self._settings.benchmarks_dir / "fixtures",
            repositories=run.repositories,
            questions=questions,
            work_dir=self._settings.data_dir / "benchmark" / run.id / "work",
            approaches=run.approaches,
            judge=run.judge,
            prompt_caching=self._needs_api_key,  # scripted test models reject cache options
            github_client=self.github_client(),
            github_sources=github_sources(
                self._settings.benchmark_github_repo,
                run.github_ref,
                self._settings.benchmark_github_root,
                run.repositories,
            )
            if token
            else None,
            embedder_factory=self.embedder_factory(),
            on_progress=lambda event: self._publish(run, event),
        )
        # The runner is async only so it can stream the agent; give it its own event loop
        # on this worker thread so blocking model calls never stall the API server.
        return asyncio.run(BenchmarkRunner(config, self._model_factory).run())

    def _save_report(self, run: BenchmarkRun, result: BenchmarkResult) -> None:
        run_info = {
            "started_at": run.started_at,
            "model": self._settings.llm_model,
            "effort": self._settings.llm_effort,
            "questions": len(run.question_ids),
            "github_ref": f"{self._settings.benchmark_github_repo}@{run.github_ref}"
            if self._github_token
            else None,
        }
        try:
            out_dir = self._settings.data_dir / "benchmark" / run.id
            write_report(result, out_dir, run_info)
        except OSError:
            logger.warning("Could not write the report for benchmark run %s", run.id, exc_info=True)
