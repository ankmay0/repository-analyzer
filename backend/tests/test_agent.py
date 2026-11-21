"""The hub: delegation to repository experts, streaming, and conversation state."""

from __future__ import annotations

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from repo_analyzer.agent.service import AgentRole, ChatEvent, ChatService
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace
from support import ScriptedChatModel, consult, expert_repo, searching_expert, tool_call


def make_service(
    hub: ScriptedChatModel,
    expert: ScriptedChatModel,
    settings: Settings,
    store: VectorStore,
    workspace: Workspace,
) -> ChatService:
    def factory(role: AgentRole) -> ScriptedChatModel:
        return hub if role == "hub" else expert

    knowledge = KnowledgeStore(settings.knowledge_dir)
    return ChatService(factory, settings, workspace, knowledge, store, prompt_caching=False)


async def collect(service: ChatService, thread_id: str, message: str) -> list[ChatEvent]:
    return [event async for event in service.stream(thread_id, message)]


def answer_text(events: list[ChatEvent]) -> str:
    return "".join(e.data["text"] for e in events if e.type == "token")


async def test_hub_delegates_to_experts_in_parallel_and_answers_from_their_reports(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    hub = ScriptedChatModel(
        responses=[
            tool_call("analyze_impact", {"repo": "core-lib"}),
            AIMessage(
                content="",
                tool_calls=[
                    *consult("mid-lib", "Where is sign_token used?").tool_calls,
                    *consult("web-app", "Where is sign_token used?").tool_calls,
                ],
            ),
            AIMessage(content="mid-lib uses it; web-app does not."),
        ]
    )
    expert = ScriptedChatModel(responder=searching_expert())
    service = make_service(hub, expert, settings, store, indexed_workspace)

    events = await collect(service, "t1", "What breaks if core-lib fails, and where?")

    # Hub steps, in order: the graph lookup, then both delegations issued together.
    hub_steps = [
        (e.data["name"], e.data["args"].get("repo")) for e in events if e.type == "tool_call"
    ]
    assert hub_steps == [
        ("analyze_impact", "core-lib"),
        ("consult_repository", "mid-lib"),
        ("consult_repository", "web-app"),
    ]
    # Each expert's own research is relayed, labelled with its repository.
    expert_steps = {(e.data["repo"], e.data["name"]) for e in events if e.type == "expert_step"}
    assert expert_steps == {("mid-lib", "find_references"), ("web-app", "find_references")}
    # Only the hub's words reach the user; the experts' reports are not streamed as answer.
    assert answer_text(events) == "mid-lib uses it; web-app does not."
    assert events[-1].type == "done"
    assert not [e for e in events if e.type == "error"]

    # Files the experts found are surfaced as sources: mid-lib's match is there, and
    # web-app contributes nothing beyond the manifest that analyze_impact cited.
    sources = {
        (s["repo"], s["path"]) for e in events if e.type == "sources" for s in e.data["sources"]
    }
    assert ("mid-lib", "src/mid_lib/session.py") in sources
    assert {path for repo, path in sources if repo == "web-app"} == {"pyproject.toml"}

    # The hub received each expert's report as the result of its delegation...
    reports = {m.tool_call_id: str(m.content) for m in hub.calls[-1] if isinstance(m, ToolMessage)}
    assert reports["ask-mid-lib"] == "Report from the mid-lib expert:\n\nmid-lib uses sign_token."
    assert reports["ask-web-app"].endswith("web-app does not use sign_token.")
    # ...but never the raw search output the experts worked from.
    assert not any("session.py:1:" in str(m.content) for call in hub.calls for m in call)


async def test_each_expert_sees_only_the_hubs_question_not_the_conversation(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    hub = ScriptedChatModel(
        responses=[consult("mid-lib", "How are sessions created?"), AIMessage(content="done")]
    )
    expert = ScriptedChatModel(responder=lambda messages: AIMessage(content="via create_session"))
    service = make_service(hub, expert, settings, store, indexed_workspace)

    await collect(service, "t1", "The user's original wording, with private context")

    (expert_call,) = expert.calls
    assert expert_repo(expert_call) == "mid-lib"
    assert [m.content for m in expert_call if isinstance(m, HumanMessage)] == [
        "How are sessions created?"
    ]
    assert isinstance(hub.calls[0][0], SystemMessage)
    assert "You cannot read code yourself" in str(hub.calls[0][0].content)


async def test_one_expert_failing_does_not_lose_the_others_report(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    def flaky(messages: list[BaseMessage]) -> AIMessage:
        if expert_repo(messages) == "web-app":
            return AIMessage(content="")  # an expert that comes back with nothing
        return AIMessage(content="mid-lib report")

    hub = ScriptedChatModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    *consult("mid-lib", "q").tool_calls,
                    *consult("web-app", "q").tool_calls,
                ],
            ),
            AIMessage(content="partial answer"),
        ]
    )
    service = make_service(
        hub, ScriptedChatModel(responder=flaky), settings, store, indexed_workspace
    )

    events = await collect(service, "t1", "question")

    assert answer_text(events) == "partial answer"
    reports = {m.tool_call_id: str(m.content) for m in hub.calls[-1] if isinstance(m, ToolMessage)}
    assert reports["ask-mid-lib"].endswith("mid-lib report")
    assert reports["ask-web-app"].startswith("Report from the web-app expert (incomplete)")


async def test_consulting_an_unknown_repository_is_reported_back_to_the_hub(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    hub = ScriptedChatModel(responses=[consult("nope", "q"), AIMessage(content="no such repo")])
    expert = ScriptedChatModel(responder=lambda messages: AIMessage(content="unused"))
    service = make_service(hub, expert, settings, store, indexed_workspace)

    events = await collect(service, "t1", "question")

    assert answer_text(events) == "no such repo"
    assert expert.calls == []
    assert "Error: Unknown repository 'nope'" in str(hub.calls[-1][-1].content)


async def test_conversation_history_is_kept_per_thread(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    hub = ScriptedChatModel(
        responses=[AIMessage(content="one"), AIMessage(content="two"), AIMessage(content="three")]
    )
    service = make_service(hub, ScriptedChatModel(), settings, store, indexed_workspace)

    await collect(service, "thread-a", "first question")
    await collect(service, "thread-a", "follow-up")
    await collect(service, "thread-b", "unrelated")

    def human_texts(call: list[BaseMessage]) -> list[str]:
        return [str(m.content) for m in call if isinstance(m, HumanMessage)]

    assert human_texts(hub.calls[1]) == ["first question", "follow-up"]
    assert human_texts(hub.calls[2]) == ["unrelated"]


async def test_runaway_hub_loops_stop_at_the_step_limit(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    limited = settings.model_copy(update={"agent_recursion_limit": 4})
    hub = ScriptedChatModel(
        responses=[tool_call("list_repositories", {}, call_id=f"c{n}") for n in range(20)]
    )
    service = make_service(hub, ScriptedChatModel(), limited, store, indexed_workspace)

    events = await collect(service, "t1", "loop forever")

    assert [e.type for e in events[-2:]] == ["error", "done"]
    assert "more research steps" in events[-2].data["message"]


async def test_a_turn_interrupted_before_its_tools_ran_does_not_poison_the_thread(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    hub = ScriptedChatModel(
        responses=[
            tool_call("list_repositories", {}, call_id="dangling"),
            AIMessage(content="recovered"),
        ]
    )
    service = make_service(hub, ScriptedChatModel(), settings, store, indexed_workspace)

    # Simulate a client disconnect right after the hub asked for a tool.
    stream = service.stream("t1", "first")
    async for event in stream:
        if event.type == "tool_call":
            break
    await stream.aclose()

    events = await collect(service, "t1", "second")

    assert answer_text(events) == "recovered"
    history = hub.calls[-1]
    dangling = [m for m in history if isinstance(m, ToolMessage) and m.tool_call_id == "dangling"]
    assert len(dangling) == 1  # every tool call in the history has exactly one result


async def test_missing_credentials_surface_as_an_error_event(
    settings: Settings, store: VectorStore, indexed_workspace: Workspace
) -> None:
    def no_model(role: AgentRole) -> ScriptedChatModel:
        raise LLMNotConfiguredError("ANTHROPIC_API_KEY is not set")

    service = ChatService(
        no_model, settings, indexed_workspace, KnowledgeStore(settings.knowledge_dir), store
    )

    events = await collect(service, "t1", "hello")

    assert [e.type for e in events] == ["error", "done"]
    assert "ANTHROPIC_API_KEY" in events[0].data["message"]
