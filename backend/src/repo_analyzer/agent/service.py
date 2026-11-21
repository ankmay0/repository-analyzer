"""Run one conversational turn and translate graph output into UI events.

The HTTP layer knows nothing about LangGraph: it consumes the ``ChatEvent``
stream produced here and writes it out as server-sent events.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from repo_analyzer.agent.diagrams import DIAGRAM_EVENT, build_diagram_tools
from repo_analyzer.agent.experts import RepositoryExperts
from repo_analyzer.agent.graph import AGENT_NODE, TOOLS_NODE, AgentGraph
from repo_analyzer.agent.hub import EXPERT_STEP_EVENT, build_hub_graph
from repo_analyzer.agent.tools import (
    build_json_repository_tools,
    build_rag_repository_tools,
    build_usage_tool,
)
from repo_analyzer.config import Settings
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace

logger = logging.getLogger(__name__)

EventType = Literal["token", "tool_call", "expert_step", "diagram", "sources", "error", "done"]
AgentRole = Literal["hub", "expert"]
ModelFactory = Callable[[AgentRole], BaseChatModel]


@dataclass(frozen=True, slots=True)
class ChatEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


class ChatService:
    def __init__(
        self,
        model_factory: ModelFactory,
        settings: Settings,
        workspace: Workspace,
        knowledge: KnowledgeStore,
        store: VectorStore | None,
        *,
        prompt_caching: bool = True,
    ) -> None:
        if settings.knowledge_mode == "rag" and store is None:
            raise ValueError("rag mode requires a vector store")
        self._model_factory = model_factory
        self._settings = settings
        self._workspace = workspace
        self._knowledge = knowledge
        self._store = store
        self._prompt_caching = prompt_caching
        # The hub's conversation history lives in process memory: it is lost on restart
        # and is not shared between workers. Swap in a durable checkpointer to change
        # that. Experts keep no history at all.
        self._checkpointer = InMemorySaver()
        self._graph: AgentGraph | None = None

    def ensure_ready(self) -> None:
        """Build the agents on first use. Raises ``LLMNotConfiguredError`` without a key."""
        if self._graph is None:
            experts = RepositoryExperts(
                self._model_factory("expert"),
                self._settings,
                self._workspace,
                self._expert_tools,
                prompt_caching=self._prompt_caching,
            )
            json_mode = self._settings.knowledge_mode == "json"
            self._graph = build_hub_graph(
                self._model_factory("hub"),
                experts,
                self._workspace,
                self._checkpointer,
                knowledge_mode=self._settings.knowledge_mode,
                extra_tools=[
                    *([build_usage_tool(self._workspace, self._knowledge)] if json_mode else []),
                    *build_diagram_tools(self._workspace),
                ],
                prompt_caching=self._prompt_caching,
            )

    def _expert_tools(self, repo: str) -> list[BaseTool]:
        if self._store is not None:
            return build_rag_repository_tools(self._settings, self._store, self._workspace, repo)
        return build_json_repository_tools(self._settings, self._workspace, self._knowledge, repo)

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[ChatEvent]:
        """Answer ``message`` in conversation ``thread_id``, yielding events as they occur.

        Always ends with exactly one ``done`` event; failures are reported as an
        ``error`` event first rather than raised, because by then the HTTP
        response has already started.
        """
        try:
            self.ensure_ready()
            assert self._graph is not None  # noqa: S101 - narrowed for the type checker
            config: RunnableConfig = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": self._settings.agent_recursion_limit,
            }
            await self._close_interrupted_turn(self._graph, config)
            async for part in self._graph.astream(
                {"messages": [HumanMessage(message)]},
                config,
                stream_mode=["messages", "updates", "custom"],
                version="v2",
            ):
                for event in _to_events(part["type"], part["data"]):
                    yield event
        except AnalyzerError as exc:
            yield ChatEvent("error", {"message": str(exc)})
        except GraphRecursionError:
            yield ChatEvent(
                "error",
                {
                    "message": "The question needed more research steps than the configured "
                    "limit. Try a narrower question."
                },
            )
        except anthropic.AuthenticationError:
            yield ChatEvent("error", {"message": "The Anthropic API key was rejected."})
        except anthropic.RateLimitError:
            yield ChatEvent(
                "error", {"message": "The model is rate limited right now. Try again shortly."}
            )
        except anthropic.APIConnectionError:
            yield ChatEvent("error", {"message": "Could not reach the Anthropic API."})
        except anthropic.APIStatusError as exc:
            logger.warning("Anthropic API error %s: %s", exc.status_code, exc.message)
            yield ChatEvent(
                "error", {"message": f"The model request failed (HTTP {exc.status_code})."}
            )
        except Exception:
            logger.exception("Chat turn failed unexpectedly")
            yield ChatEvent("error", {"message": "Unexpected server error; see server logs."})
        yield ChatEvent("done")

    @staticmethod
    async def _close_interrupted_turn(graph: AgentGraph, config: RunnableConfig) -> None:
        """Repair a conversation whose previous turn stopped before its tools ran.

        If a client disconnects mid-turn, the saved history can end with a model
        message requesting tool calls that never received results. The Anthropic
        API rejects such a history, so every later question in that conversation
        would fail. Answer the dangling calls with a placeholder result instead.
        """
        snapshot = await graph.aget_state(config)
        messages = snapshot.values.get("messages", [])
        if not messages or not isinstance(messages[-1], AIMessage) or not messages[-1].tool_calls:
            return
        placeholders = [
            ToolMessage(
                content="Not executed: the previous request was interrupted.",
                tool_call_id=call["id"],
            )
            for call in messages[-1].tool_calls
        ]
        await graph.aupdate_state(config, {"messages": placeholders}, as_node=TOOLS_NODE)


def _to_events(kind: str, data: Any) -> list[ChatEvent]:
    """Map one stream part from the hub graph to UI events.

    ``messages`` carries the hub's answer text, ``updates`` its completed steps, and
    ``custom`` what tools publish while they run: experts' progress and diagrams.
    """
    if kind == "custom":
        if isinstance(data, dict) and data.get("type") == EXPERT_STEP_EVENT:
            step = {key: data[key] for key in ("repo", "name", "args")}
            return [ChatEvent("expert_step", step)]
        if isinstance(data, dict) and data.get("type") == DIAGRAM_EVENT:
            return [ChatEvent("diagram", data["diagram"])]
        return []
    if kind == "messages":
        chunk, metadata = data
        if isinstance(chunk, AIMessageChunk) and metadata.get("langgraph_node") == AGENT_NODE:
            text = str(chunk.text)
            if text:
                return [ChatEvent("token", {"text": text})]
        return []

    events: list[ChatEvent] = []
    for node, update in data.items():
        for message in (update or {}).get("messages", []):
            if node == AGENT_NODE and isinstance(message, AIMessage):
                events += [
                    ChatEvent("tool_call", {"name": call["name"], "args": call["args"]})
                    for call in message.tool_calls
                ]
                events += _stop_reason_events(message)
            elif node == TOOLS_NODE and isinstance(message, ToolMessage) and message.artifact:
                events.append(ChatEvent("sources", {"sources": message.artifact}))
    return events


def _stop_reason_events(message: AIMessage) -> list[ChatEvent]:
    stop_reason = message.response_metadata.get("stop_reason")
    if stop_reason == "refusal":
        return [ChatEvent("error", {"message": "The model declined to answer this request."})]
    if stop_reason == "max_tokens":
        return [
            ChatEvent(
                "error",
                {"message": "The answer was cut off at the output limit (RA_LLM_MAX_TOKENS)."},
            )
        ]
    return []
