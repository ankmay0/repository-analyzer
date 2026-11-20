"""Repository experts: the spokes of the hub-and-spoke design.

An expert is an agent loop restricted to one repository. It is stateless: each
consultation starts with only the hub's question, runs its own search-and-read
loop, and returns a written report. The intermediate search results stay in the
expert's context and never reach the hub, which is what keeps the hub's context
small as the number and size of repositories grows.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anthropic
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from repo_analyzer.agent.graph import AGENT_NODE, TOOLS_NODE, AgentGraph, build_tool_loop
from repo_analyzer.agent.prompts import expert_prompt
from repo_analyzer.agent.tools import Source, build_repository_tools
from repo_analyzer.config import Settings
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import Workspace

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExpertStep:
    """One tool call made by an expert, reported while the consultation runs."""

    repo: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ExpertReport:
    repo: str
    text: str
    sources: list[Source]
    failed: bool = False


StepCallback = Callable[[ExpertStep], None]


class RepositoryExperts:
    def __init__(
        self,
        model: BaseChatModel,
        settings: Settings,
        store: VectorStore,
        workspace: Workspace,
        *,
        prompt_caching: bool = True,
    ) -> None:
        self._model = model
        self._settings = settings
        self._store = store
        self._workspace = workspace
        self._prompt_caching = prompt_caching
        self._graphs: dict[str, AgentGraph] = {}
        # The hub may consult every expert at once; this bounds concurrent model use.
        self._slots = asyncio.Semaphore(settings.max_parallel_experts)

    async def consult(
        self, repo: str, question: str, on_step: StepCallback = lambda _: None
    ) -> ExpertReport:
        """Put ``question`` to the expert on ``repo`` and return its report.

        Raises ``UnknownRepositoryError`` if ``repo`` is not indexed. A failure
        inside the consultation is returned as a failed report rather than
        raised, so that one expert failing does not discard what the experts
        consulted alongside it found.
        """
        self._workspace.state.repository(repo)
        sources: list[Source] = []
        answer = ""
        async with self._slots:
            try:
                async for part in self._graph_for(repo).astream(
                    {"messages": [HumanMessage(question)]},
                    {"recursion_limit": self._settings.expert_recursion_limit},
                    stream_mode="updates",
                    version="v2",
                ):
                    updates: Any = part["data"]
                    for node, update in updates.items():
                        for message in (update or {}).get("messages", []):
                            if node == AGENT_NODE and isinstance(message, AIMessage):
                                for call in message.tool_calls:
                                    on_step(ExpertStep(repo, call["name"], call["args"]))
                                answer = str(message.text)
                            elif node == TOOLS_NODE and isinstance(message, ToolMessage):
                                sources += message.artifact or []
            except GraphRecursionError:
                return ExpertReport(
                    repo,
                    f"The {repo} expert used all of its research steps without reaching a "
                    "conclusion. Ask a narrower question.",
                    sources,
                    failed=True,
                )
            except anthropic.APIError as exc:
                logger.warning("The %s expert failed: %s", repo, exc)
                return ExpertReport(
                    repo, f"The {repo} expert could not complete the request.", sources, failed=True
                )
        if not answer.strip():
            return ExpertReport(repo, f"The {repo} expert returned no findings.", sources, True)
        return ExpertReport(repo, answer, sources)

    def _graph_for(self, repo: str) -> AgentGraph:
        # Safe to cache by name across re-indexing: the tools look the repository up
        # in the current workspace on every call rather than capturing its location.
        if repo not in self._graphs:
            self._graphs[repo] = build_tool_loop(
                self._model,
                build_repository_tools(self._settings, self._store, self._workspace, repo),
                expert_prompt(repo),
                checkpointer=None,
                stream_tokens=False,
                prompt_caching=self._prompt_caching,
            )
        return self._graphs[repo]
