"""Benchmark routes: start a run, read its progress, download its results."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from repo_analyzer.api.routes import ServicesDep
from repo_analyzer.benchmark.service import BenchmarkRunRequest

router = APIRouter(tags=["benchmark"])


class BenchmarkRunIn(BaseModel):
    approaches: list[str] = Field(min_length=1, description="Any of the four approach names.")
    question_ids: list[str] | None = Field(
        default=None, description="Subset of questions; all of them when omitted."
    )
    repositories: list[str] | None = Field(
        default=None, description="Subset of fixture apps to index; all when omitted."
    )
    judge: bool = Field(default=False, description="Also grade each answer with an LLM judge.")
    github_ref: str | None = Field(
        default=None,
        max_length=200,
        description="Branch, tag or commit json-github reads (default RA_BENCHMARK_GITHUB_REF).",
    )


@router.get("/status")
def benchmark_status(services: ServicesDep) -> dict[str, Any]:
    """Whether a run can start, and which approaches are available (with the reason if not)."""
    return services.benchmark.status()


@router.get("/questions")
def benchmark_questions(services: ServicesDep) -> dict[str, Any]:
    question_set = services.benchmark.question_set()
    return {
        "repositories": list(question_set.repositories),
        "questions": [
            {
                "id": q.id,
                "category": q.category,
                "question": q.question,
                "expected_answer": q.expected_answer,
                "key_facts": list(q.key_facts),
                "evidence": list(q.evidence),
            }
            for q in question_set.questions
        ],
    }


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_benchmark_run(body: BenchmarkRunIn, services: ServicesDep) -> dict[str, Any]:
    """Start a run in the background. Poll ``GET /runs/{id}`` or stream ``/runs/{id}/events``."""
    run = services.benchmark.start(BenchmarkRunRequest(**body.model_dump()))
    return run.snapshot()


@router.get("/runs/{run_id}")
def get_benchmark_run(run_id: str, services: ServicesDep) -> dict[str, Any]:
    return services.benchmark.get(run_id).snapshot()


@router.get("/runs/{run_id}/events")
async def stream_benchmark_run(
    run_id: str,
    services: ServicesDep,
    after: Annotated[int, Query(ge=0, description="Skip this many events (resume a stream).")] = 0,
) -> EventSourceResponse:
    """Server-sent events: ``plan``, ``approach_setup``, ``approach_started``,
    ``approach_skipped``, ``question_started``, ``result``, then ``finished`` or ``failed``.
    Every event carries ``progress`` (completed and total answers)."""
    services.benchmark.get(run_id)  # 404 before the stream starts

    async def events() -> AsyncIterator[ServerSentEvent]:
        async for index, event in services.benchmark.events(run_id, after):
            yield ServerSentEvent(event=event["type"], data=json.dumps(event), id=str(index))

    return EventSourceResponse(events())


@router.get("/runs/{run_id}/results.csv")
def download_benchmark_csv(run_id: str, services: ServicesDep) -> Response:
    """The answers so far (one row per question and approach) as CSV."""
    return Response(
        services.benchmark.results_csv(run_id),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="benchmark-{run_id}.csv"'},
    )
