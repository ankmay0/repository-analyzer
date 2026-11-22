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
from langgraph.graph.state import CompiledStateGraph

from repo_analyzer.agent.graph import AGENT_NODE, TOOLS_NODE, build_agent_graph
from repo_analyzer.agent.prompts import SYSTEM_PROMPT
from repo_analyzer.agent.usage import UsageTracker
from repo_analyzer.errors import AnalyzerError

logger = logging.getLogger(__name__)

EventType = Literal["token", "tool_call", "sources", "error", "usage", "done"]


@dataclass(frozen=True, slots=True)
class ChatEvent:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)


class ChatService:
    def __init__(
        self,
        model_factory: Callable[[], BaseChatModel],
        tools: list[BaseTool],
        *,
        recursion_limit: int,
        prompt_caching: bool = True,
        system_prompt: str = SYSTEM_PROMPT,
    ) -> None:
        self._model_factory = model_factory
        self._tools = tools
        self._recursion_limit = recursion_limit
        self._prompt_caching = prompt_caching
        self._system_prompt = system_prompt
        # Conversation history lives in process memory: it is lost on restart and is
        # not shared between workers. Swap in a durable checkpointer to change that.
        self._checkpointer = InMemorySaver()
        self._graph: CompiledStateGraph[Any, Any, Any, Any] | None = None

    def ensure_ready(self) -> None:
        """Build the agent on first use. Raises ``LLMNotConfiguredError`` without a key."""
        if self._graph is None:
            self._graph = build_agent_graph(
                self._model_factory(),
                self._tools,
                self._checkpointer,
                prompt_caching=self._prompt_caching,
                system_prompt=self._system_prompt,
            )

    async def stream(self, thread_id: str, message: str) -> AsyncIterator[ChatEvent]:
        """Answer ``message`` in conversation ``thread_id``, yielding events as they occur.

        Always ends with one ``usage`` event (tokens summed over every model call,
        tool-call count and wall-clock latency) followed by exactly one ``done``
        event; failures are reported as an ``error`` event first rather than
        raised, because by then the HTTP response has already started.
        """
        usage = UsageTracker()
        try:
            self.ensure_ready()
            assert self._graph is not None  # noqa: S101 - narrowed for the type checker
            config: RunnableConfig = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": self._recursion_limit,
            }
            await self._close_interrupted_turn(self._graph, config)
            async for part in self._graph.astream(
                {"messages": [HumanMessage(message)]},
                config,
                stream_mode=["messages", "updates"],
                version="v2",
            ):
                if part["type"] == "updates":
                    _count_usage(part["data"], usage)
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
        usage.finish()
        summary = usage.to_dict()
        logger.info(
            "chat usage thread=%s input=%d output=%d total=%d cache_read=%d cache_creation=%d "
            "llm_calls=%d tool_calls=%d latency_ms=%d",
            thread_id,
            summary["input_tokens"],
            summary["output_tokens"],
            summary["total_tokens"],
            summary["cache_read_tokens"],
            summary["cache_creation_tokens"],
            summary["llm_calls"],
            summary["tool_calls"],
            summary["latency_ms"],
            extra={"chat_usage": {"thread_id": thread_id, **summary}},
        )
        yield ChatEvent("usage", summary)
        yield ChatEvent("done")

    @staticmethod
    async def _close_interrupted_turn(
        graph: CompiledStateGraph[Any, Any, Any, Any], config: RunnableConfig
    ) -> None:
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


def _count_usage(data: Any, usage: UsageTracker) -> None:
    """Add every model response in one ``updates`` stream part to ``usage``."""
    for node, update in data.items():
        if node != AGENT_NODE:
            continue
        for message in (update or {}).get("messages", []):
            if isinstance(message, AIMessage):
                usage.add_model_response(message)


def _to_events(kind: str, data: Any) -> list[ChatEvent]:
    """Map one LangGraph stream part (``messages`` or ``updates`` mode) to UI events."""
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
