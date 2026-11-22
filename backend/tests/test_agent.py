from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from repo_analyzer.agent.service import ChatEvent, ChatService
from repo_analyzer.agent.tools import build_tools
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.workspace import Workspace
from support import ScriptedChatModel, tool_call


def make_service(
    model: ScriptedChatModel,
    settings: Settings,
    index_store: RepoIndexStore,
    workspace: Workspace,
    recursion_limit: int = 10,
) -> ChatService:
    return ChatService(
        lambda: model,
        build_tools(settings, index_store, workspace),
        recursion_limit=recursion_limit,
        prompt_caching=False,
    )


async def collect(service: ChatService, thread_id: str, message: str) -> list[ChatEvent]:
    return [event async for event in service.stream(thread_id, message)]


async def test_agent_calls_a_tool_then_answers_from_its_result(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("get_repository_info", {"repo": "mid-lib"}),
            AIMessage(content="mid-lib depends on core-lib."),
        ]
    )
    service = make_service(model, settings, index_store, indexed_workspace)

    events = await collect(service, "t1", "What does mid-lib depend on?")

    assert [e.type for e in events] == ["tool_call", "sources", "token", "token", "done"]
    assert events[0].data == {"name": "get_repository_info", "args": {"repo": "mid-lib"}}
    assert {s["repo"] for s in events[1].data["sources"]} == {"mid-lib"}
    assert "".join(e.data["text"] for e in events if e.type == "token") == (
        "mid-lib depends on core-lib."
    )

    # The second model call saw the system prompt, the question and the tool's output.
    first, second = model.calls
    assert isinstance(first[0], SystemMessage) and isinstance(first[1], HumanMessage)
    tool_result = second[-1]
    assert isinstance(tool_result, ToolMessage)
    assert '"name":"core-lib"' in str(tool_result.content)


async def test_conversation_history_is_kept_per_thread(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(
        responses=[AIMessage(content="one"), AIMessage(content="two"), AIMessage(content="three")]
    )
    service = make_service(model, settings, index_store, indexed_workspace)

    await collect(service, "thread-a", "first question")
    await collect(service, "thread-a", "follow-up")
    await collect(service, "thread-b", "unrelated")

    def human_texts(call: list) -> list[str]:  # type: ignore[type-arg]
        return [str(m.content) for m in call if isinstance(m, HumanMessage)]

    assert human_texts(model.calls[1]) == ["first question", "follow-up"]
    assert human_texts(model.calls[2]) == ["unrelated"]


async def test_runaway_tool_loops_stop_at_the_step_limit(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(
        responses=[tool_call("list_repositories", {}, call_id=f"c{n}") for n in range(20)]
    )
    service = make_service(model, settings, index_store, indexed_workspace, recursion_limit=4)

    events = await collect(service, "t1", "loop forever")

    assert [e.type for e in events[-2:]] == ["error", "done"]
    assert "more research steps" in events[-2].data["message"]


async def test_a_turn_interrupted_before_its_tools_ran_does_not_poison_the_thread(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> None:
    model = ScriptedChatModel(
        responses=[
            tool_call("list_repositories", {}, call_id="dangling"),
            AIMessage(content="recovered"),
        ]
    )
    service = make_service(model, settings, index_store, indexed_workspace)

    # Simulate a client disconnect right after the model asked for a tool.
    stream = service.stream("t1", "first")
    async for event in stream:
        if event.type == "tool_call":
            break
    await stream.aclose()

    events = await collect(service, "t1", "second")

    assert "".join(e.data["text"] for e in events if e.type == "token") == "recovered"
    history = model.calls[-1]
    dangling = [m for m in history if isinstance(m, ToolMessage) and m.tool_call_id == "dangling"]
    assert len(dangling) == 1  # every tool call in the history has exactly one result


async def test_missing_credentials_surface_as_an_error_event(
    settings: Settings, index_store: RepoIndexStore, indexed_workspace: Workspace
) -> None:
    def no_model() -> ScriptedChatModel:
        raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set")

    service = ChatService(
        no_model, build_tools(settings, index_store, indexed_workspace), recursion_limit=5
    )

    events = await collect(service, "t1", "hello")

    assert [e.type for e in events] == ["error", "done"]
    assert "ANTHROPIC_API_KEY" in events[0].data["message"]
