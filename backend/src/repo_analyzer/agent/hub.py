"""The hub: the coordinating agent of the hub-and-spoke design.

    user ──► hub ──► consult_repository ──► expert on repository A
              │                        ├──► expert on repository B   (in parallel)
              │                        └──► ...
              └──► list_repositories / analyze_impact / find_usages   (graph, no code)

The hub holds the conversation with the user and the dependency graph. It has no
tool that returns code: anything it learns about a repository's contents comes
from that repository's expert. Experts never communicate with each other.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.config import get_stream_writer

from repo_analyzer.agent.experts import ExpertStep, RepositoryExperts
from repo_analyzer.agent.graph import AgentGraph, build_tool_loop
from repo_analyzer.agent.prompts import hub_prompt
from repo_analyzer.agent.tools import ToolOutput, build_graph_tools
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.workspace import Workspace

CONSULT_TOOL = "consult_repository"
EXPERT_STEP_EVENT = "expert_step"


def build_consult_tool(experts: RepositoryExperts) -> BaseTool:
    @tool(CONSULT_TOOL, response_format="content_and_artifact")
    async def consult_repository(repo: str, question: str) -> ToolOutput:
        """Ask the expert on one repository a question and get back a cited report.

        The expert can search and read that repository only, and sees nothing but
        this question, so the question must be complete without other context.

        Args:
            repo: Name of an indexed repository, as shown by list_repositories.
            question: A self-contained question about that repository.
        """
        # Relay the expert's tool calls to whoever is streaming the hub, so the user
        # can watch the research happen instead of waiting on a silent delegation.
        writer = get_stream_writer()

        def relay(step: ExpertStep) -> None:
            writer(
                {"type": EXPERT_STEP_EVENT, "repo": step.repo, "name": step.name, "args": step.args}
            )

        try:
            report = await experts.consult(repo, question, relay)
        except AnalyzerError as exc:
            return f"Error: {exc}", []
        heading = f"Report from the {repo} expert"
        return (
            f"{heading}{' (incomplete)' if report.failed else ''}:\n\n{report.text}",
            report.sources,
        )

    return consult_repository


def build_hub_graph(
    model: BaseChatModel,
    experts: RepositoryExperts,
    workspace: Workspace,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    knowledge_mode: str,
    extra_tools: Sequence[BaseTool] = (),
    prompt_caching: bool = True,
) -> AgentGraph:
    return build_tool_loop(
        model,
        [*build_graph_tools(workspace), *extra_tools, build_consult_tool(experts)],
        hub_prompt(knowledge_mode),
        checkpointer=checkpointer,
        stream_tokens=True,
        prompt_caching=prompt_caching,
    )
