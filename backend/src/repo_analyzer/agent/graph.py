"""The agent loop shared by the hub and the repository experts.

    START -> agent -> (tool calls?) -> tools -> agent -> ... -> END

A model node and a tool node in a loop: the model decides which tools to call
and when it has enough to answer. The hub and every expert are instances of this
same loop, differing only in system prompt, tools and whether they keep state.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import TAG_NOSTREAM
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

AGENT_NODE = "agent"
TOOLS_NODE = "tools"

AgentGraph = CompiledStateGraph[Any, Any, Any, Any]

# Marks the end of the conversation so far as a prompt-cache breakpoint. Within one
# question an agent calls the model several times with a growing transcript of
# tool results; caching means each call pays full price only for what is new.
_CACHE_CONTROL = {"type": "ephemeral"}


def build_tool_loop(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    *,
    checkpointer: BaseCheckpointSaver[Any] | None,
    stream_tokens: bool,
    prompt_caching: bool = True,
) -> AgentGraph:
    """Compile a model-and-tools loop.

    Args:
        checkpointer: Where conversation state is kept between turns. ``None`` makes
            the loop stateless: every invocation starts from an empty history, and
            it never inherits a checkpointer from a graph it is invoked inside.
        stream_tokens: Whether the model's text is published on the ``messages``
            stream of the outermost running graph. Off for experts, whose text is
            a report for the hub and must not be mixed into the user's answer.
    """
    runnable = model.bind_tools(list(tools))
    if not stream_tokens:
        runnable = runnable.with_config(tags=[TAG_NOSTREAM])
    call_options: dict[str, Any] = {"cache_control": _CACHE_CONTROL} if prompt_caching else {}
    system_message = SystemMessage(system_prompt)

    async def call_model(state: MessagesState) -> dict[str, Any]:
        response = await runnable.ainvoke([system_message, *state["messages"]], **call_options)
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node(AGENT_NODE, call_model)
    builder.add_node(TOOLS_NODE, ToolNode(list(tools)))
    builder.add_edge(START, AGENT_NODE)
    builder.add_conditional_edges(AGENT_NODE, tools_condition)
    builder.add_edge(TOOLS_NODE, AGENT_NODE)
    return builder.compile(checkpointer=checkpointer if checkpointer is not None else False)
