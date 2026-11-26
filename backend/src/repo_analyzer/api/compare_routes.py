"""Compare route: one question, every approach side by side, streamed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse, ServerSentEvent

from repo_analyzer.api.routes import ServicesDep
from repo_analyzer.benchmark.compare import FIXTURES_ID, CompareRequest

router = APIRouter(tags=["compare"])


class CompareIn(BaseModel):
    question: str = Field(min_length=1, max_length=8_000)
    repository: str = Field(
        default=FIXTURES_ID,
        min_length=1,
        max_length=200,
        description=f"An indexed repository's name, or {FIXTURES_ID!r} for the benchmark fixtures.",
    )
    approaches: list[str] | None = Field(
        default=None, description="Subset of approaches; all five when omitted."
    )


@router.get("/status")
def compare_status(services: ServicesDep) -> dict[str, Any]:
    """Repositories that can be compared, and which approaches can run (with the reason)."""
    return services.compare.status()


@router.post("")
async def compare(body: CompareIn, services: ServicesDep) -> EventSourceResponse:
    """Answer one question under every approach at once, as a server-sent event stream.

    Every event carries ``approach`` (except ``plan`` and ``done``). Events: ``plan`` (the
    approaches, available or not, with the reason), ``setup`` and ``started`` (preparing),
    ``token`` (answer text), ``tool_call``, ``error``, ``unavailable``, ``usage`` (input,
    output and cache tokens, latency, tool calls), ``approach_done``, and a final ``done``.
    """
    request = CompareRequest(**body.model_dump())
    # Fail with a proper status code while that is still possible (no API key, unknown
    # repository); preparing the fixtures can take a few seconds, so keep it off the loop.
    target = await asyncio.to_thread(services.compare.resolve, request)

    async def events() -> AsyncIterator[ServerSentEvent]:
        async for event in services.compare.events(target, request):
            yield ServerSentEvent(event=event["type"], data=json.dumps(event))

    return EventSourceResponse(events())
