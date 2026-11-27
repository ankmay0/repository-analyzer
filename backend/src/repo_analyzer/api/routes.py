"""HTTP routes. Handlers translate between HTTP and the service layer only."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sse_starlette import EventSourceResponse, ServerSentEvent

from repo_analyzer.api.schemas import (
    ChatRequest,
    DirectAskRequest,
    DirectAskResponse,
    HealthOut,
    IndexRequest,
    SkillOut,
    WorkspaceOut,
)
from repo_analyzer.api.services import Services
from repo_analyzer.direct_agent import ask_direct
from repo_analyzer.errors import WorkspaceEmptyError
from repo_analyzer.ingestion.symbols import probe_ctags

router = APIRouter()


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


@router.get("/health")
def health(services: ServicesDep) -> HealthOut:
    version = probe_ctags(services.settings.ctags_path)
    key = services.settings.anthropic_api_key
    graph = services.graph_store
    return HealthOut(
        status="ok",
        llm_configured=key is not None and bool(key.get_secret_value()),
        llm_model=services.settings.llm_model,
        ctags_available=version is not None,
        ctags_version=version,
        graph_available=graph is not None and graph.ping(),
    )


@router.get("/skills")
def list_skills(services: ServicesDep) -> list[SkillOut]:
    """Installed Agent Skills (name and description). The bodies load via the chat tool."""
    return [
        SkillOut(name=skill.name, description=skill.description) for skill in services.skills.list()
    ]


@router.get("/workspace")
def get_workspace(services: ServicesDep) -> WorkspaceOut:
    return WorkspaceOut.build(services.workspace.state, services.jobs.status)


@router.post("/workspace/index", status_code=status.HTTP_202_ACCEPTED)
async def start_indexing(body: IndexRequest, services: ServicesDep) -> WorkspaceOut:
    """Replace the workspace with the given repositories. Poll GET /workspace for progress."""
    services.jobs.start(body.repositories)
    return WorkspaceOut.build(services.workspace.state, services.jobs.status)


@router.post("/chat")
async def chat(body: ChatRequest, services: ServicesDep) -> EventSourceResponse:
    """Answer a question as a server-sent event stream.

    Events: ``token`` (answer text), ``tool_call`` (an agent step), ``sources``
    (file ranges the step returned), ``error``, ``usage`` (tokens and latency for the
    answer), and a final ``done``.
    """
    # Fail with a proper status code while that is still possible; once the stream
    # has started, errors can only be reported as events.
    if not services.workspace.state.repositories:
        raise WorkspaceEmptyError("Index at least one repository before asking questions")
    services.chat.ensure_ready()

    async def events() -> AsyncIterator[ServerSentEvent]:
        async for event in services.chat.stream(body.thread_id, body.message):
            yield ServerSentEvent(event=event.type, data=json.dumps(event.data))

    return EventSourceResponse(events())



@router.post("/direct/ask")
async def direct_ask(body: DirectAskRequest, services: ServicesDep) -> DirectAskResponse:
    """Answer a question by reading the cloned repository files directly.

    No Ctags JSON index or Neo4j graph is used. ``repo`` is a git URL, a local
    path (if ``RA_ALLOW_LOCAL_PATHS`` is on), or the name of an existing checkout.
    """
    result = await ask_direct(
        body.repo,
        body.question,
        settings=services.settings,
        fetcher=services.fetcher,
        workspace=services.workspace,
        model=services.model_factory(),
    )
    return DirectAskResponse.model_validate(result.to_dict())
