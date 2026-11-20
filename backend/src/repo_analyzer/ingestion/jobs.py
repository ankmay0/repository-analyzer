"""Run indexing in the background and expose its progress.

Indexing is CPU- and I/O-bound and takes far longer than an HTTP request should
block, so it runs on a worker thread while the API reports status. One job at a
time: the pipeline rewrites shared state, and a single-process, single-workspace
deployment has no use for a queue.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum

from repo_analyzer.errors import AnalyzerError, IndexingInProgressError
from repo_analyzer.ingestion.pipeline import IndexingService

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class JobStatus:
    state: JobState = JobState.IDLE
    message: str = ""
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class IndexJobRunner:
    def __init__(self, service: IndexingService) -> None:
        self._service = service
        self._lock = threading.Lock()
        self._status = JobStatus()
        self._task: asyncio.Task[None] | None = None

    @property
    def status(self) -> JobStatus:
        with self._lock:
            return self._status

    def start(self, sources: Sequence[str]) -> None:
        """Validate the request and start indexing; returns immediately."""
        self._service.validate(sources)
        with self._lock:
            if self._status.state is JobState.RUNNING:
                raise IndexingInProgressError("An indexing job is already running")
            self._status = JobStatus(state=JobState.RUNNING, message="Starting", started_at=_now())
        # Keep a reference: the event loop only holds weak references to tasks.
        self._task = asyncio.create_task(self._run(list(sources)))

    async def wait(self) -> None:
        """Wait for the current job, if any. Used by tests and graceful shutdown."""
        if self._task is not None:
            await self._task

    async def _run(self, sources: list[str]) -> None:
        try:
            await asyncio.to_thread(self._service.run, sources, self._report)
        except AnalyzerError as exc:
            self._finish(JobState.FAILED, error=str(exc))
        except Exception:
            logger.exception("Indexing failed unexpectedly")
            self._finish(JobState.FAILED, error="Indexing failed unexpectedly; see server logs")
        else:
            self._finish(JobState.SUCCEEDED)

    def _report(self, message: str) -> None:
        logger.info("%s", message)
        with self._lock:
            self._status = replace(self._status, message=message)

    def _finish(self, state: JobState, *, error: str | None = None) -> None:
        with self._lock:
            self._status = replace(self._status, state=state, error=error, finished_at=_now())
