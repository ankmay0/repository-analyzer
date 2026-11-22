"""The LangGraph agent: a model node and a tool node in a loop.

    START -> agent -> (tool calls?) -> tools -> agent -> ... -> END

The model decides which tools to call (look at a repository's JSON index, search
the code, read a file) and when it has enough evidence to answer. The loop is under
the model's control because cross-repository questions usually need several hops
(find where something is declared, then where it is used) that a fixed
retrieve-then-generate pipeline cannot plan.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import SystemMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, MessagesState, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from repo_analyzer.agent.prompts import SYSTEM_PROMPT

AGENT_NODE = "agent"
TOOLS_NODE = "tools"

# Marks the end of the conversation so far as a prompt-cache breakpoint. Within one
# question the agent calls the model several times with a growing transcript of
# tool results; caching means each call pays full price only for what is new.
_CACHE_CONTROL = {"type": "ephemeral"}


def build_agent_graph(
    model: BaseChatModel,
    tools: Sequence[BaseTool],
    checkpointer: BaseCheckpointSaver[Any],
    *,
    prompt_caching: bool = True,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    model_with_tools = model.bind_tools(list(tools))
    call_options: dict[str, Any] = {"cache_control": _CACHE_CONTROL} if prompt_caching else {}
    system_message = SystemMessage(SYSTEM_PROMPT)

    async def call_model(state: MessagesState) -> dict[str, Any]:
        response = await model_with_tools.ainvoke(
            [system_message, *state["messages"]], **call_options
        )
        return {"messages": [response]}

    builder = StateGraph(MessagesState)
    builder.add_node(AGENT_NODE, call_model)
    builder.add_node(TOOLS_NODE, ToolNode(list(tools)))
    builder.add_edge(START, AGENT_NODE)
    builder.add_conditional_edges(AGENT_NODE, tools_condition)
    builder.add_edge(TOOLS_NODE, AGENT_NODE)
    return builder.compile(checkpointer=checkpointer)
