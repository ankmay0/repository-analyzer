from __future__ import annotations

import asyncio

import anthropic
import httpx
import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from repo_analyzer.agent.experts import ExpertStep, RepositoryExperts
from repo_analyzer.config import Settings
from repo_analyzer.errors import UnknownRepositoryError
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace
from support import ScriptedChatModel, expert_repo, searching_expert, tool_call


def make_experts(
    model: ScriptedChatModel, settings: Settings, store: VectorStore, workspace: Workspace
) -> RepositoryExperts:
    return RepositoryExperts(model, settings, store, workspace, prompt_caching=False)


async def test_an_expert_researches_its_repository_and_reports_with_sources(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(responder=searching_expert())
    experts = make_experts(model, settings, store, indexed_workspace)
    steps: list[ExpertStep] = []

    report = await experts.consult("mid-lib", "Where is sign_token used?", steps.append)

    assert report.repo == "mid-lib" and not report.failed
    assert report.text == "mid-lib uses sign_token."
    assert steps == [ExpertStep("mid-lib", "find_references", {"text": "sign_token"})]
    assert {s["repo"] for s in report.sources} == {"mid-lib"}

    # The expert saw its own system prompt and the question, and nothing else.
    first_call = model.calls[0]
    assert isinstance(first_call[0], SystemMessage)
    assert "expert on the `mid-lib` repository" in str(first_call[0].content)
    assert [m.content for m in first_call[1:]] == ["Where is sign_token used?"]


async def test_consultations_are_independent_of_each_other(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(responder=lambda messages: AIMessage(content="noted"))
    experts = make_experts(model, settings, store, indexed_workspace)

    await experts.consult("mid-lib", "first question")
    await experts.consult("mid-lib", "second question")

    second_call = model.calls[1]
    assert [m.content for m in second_call if isinstance(m, HumanMessage)] == ["second question"]


async def test_experts_run_concurrently_up_to_the_configured_limit(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    limited = settings.model_copy(update={"max_parallel_experts": 2})
    running = 0
    peak = 0

    class SlowModel(ScriptedChatModel):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return self._generate(messages, stop, None, **kwargs)

        _stream = None  # type: ignore[assignment]  # force the non-streaming path above

    model = SlowModel(responder=lambda m: AIMessage(content=f"report from {expert_repo(m)}"))
    experts = make_experts(model, limited, store, indexed_workspace)

    reports = await asyncio.gather(
        *(experts.consult(repo, "q") for repo in ["core-lib", "mid-lib", "web-app", "tools"])
    )

    assert [r.text for r in reports] == [
        "report from core-lib",
        "report from mid-lib",
        "report from web-app",
        "report from tools",
    ]
    assert peak == 2


async def test_an_unknown_repository_is_rejected_before_any_model_call(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(responder=lambda messages: AIMessage(content="unused"))
    experts = make_experts(model, settings, store, indexed_workspace)

    with pytest.raises(UnknownRepositoryError):
        await experts.consult("nope", "anything")
    assert model.calls == []


async def test_an_expert_that_never_concludes_returns_a_failed_report_with_its_sources(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    limited = settings.model_copy(update={"expert_recursion_limit": 4})
    count = 0

    def always_search(messages: list[BaseMessage]) -> AIMessage:
        nonlocal count
        count += 1
        return tool_call("find_references", {"text": "sign_token"}, call_id=f"c{count}")

    experts = make_experts(
        ScriptedChatModel(responder=always_search), limited, store, indexed_workspace
    )

    report = await experts.consult("core-lib", "loop")

    assert report.failed
    assert "used all of its research steps" in report.text
    assert report.sources  # what it found before stopping is not thrown away


async def test_a_model_failure_becomes_a_failed_report_not_an_exception(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    def overloaded(messages: list[BaseMessage]) -> AIMessage:
        request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
        raise anthropic.APIStatusError(
            "overloaded", response=httpx.Response(529, request=request), body=None
        )

    experts = make_experts(
        ScriptedChatModel(responder=overloaded), settings, store, indexed_workspace
    )

    report = await experts.consult("core-lib", "q")

    assert report.failed and "could not complete the request" in report.text
