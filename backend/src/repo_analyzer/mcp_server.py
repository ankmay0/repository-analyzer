"""A small MCP server for Repository Analyzer.

MCP (Model Context Protocol) lets an AI client such as Cursor or Claude Desktop call
our code as "tools". This file only wires four tools to code that already exists in
the backend; it adds no analysis logic of its own.

Run it over stdio (the client starts it as a subprocess):

    python -m repo_analyzer.mcp_server

Do not print() here: stdout is reserved for the MCP protocol. Logs go to stderr.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from fastmcp import FastMCP

from repo_analyzer.api.services import Services, build_services
from repo_analyzer.config import Settings
from repo_analyzer.direct_agent import ask_direct as run_direct_agent
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.ingestion.symbols import probe_ctags

mcp = FastMCP(
    "Repository Analyzer",
    instructions=(
        "Questions about a few indexed repositories. Call index_repositories first, then "
        "ask_question. list_repositories shows what is indexed. trace_endpoint needs the "
        "optional Neo4j code graph."
    ),
)


@lru_cache(maxsize=1)
def get_services() -> Services:
    """The same objects the web API uses (settings, workspace, indexer, chat agent).

    Built on first use, so importing this file needs no settings. Tests replace it.
    """
    return build_services(Settings())


@mcp.tool
def list_repositories() -> list[dict[str, Any]]:
    """List the repositories that are currently indexed."""
    repos = get_services().workspace.state.repositories
    return [
        {"name": r.name, "owner": r.owner, "source": r.source, "files": r.file_count} for r in repos
    ]


@mcp.tool
async def index_repositories(repositories: list[str]) -> str:
    """Clone and index repositories (replaces the current set). Waits until it is done.

    Args:
        repositories: Git URLs, for example ["https://github.com/pallets/flask"].
    """
    jobs = get_services().jobs
    try:
        jobs.start(repositories)  # the same call as POST /api/workspace/index
    except AnalyzerError as exc:
        return f"Error: {exc}"
    await jobs.wait()
    status = jobs.status
    if status.error:
        return f"Indexing failed: {status.error}"
    return status.message  # e.g. "Indexed 2 repositories"


@mcp.tool
async def ask_question(question: str, thread_id: str = "mcp") -> str:
    """Ask the agent a question about the indexed repositories.

    It reads the JSON indexes and the code, and cites files as repo/path:line.
    Use the same thread_id to continue a conversation. Needs ANTHROPIC_API_KEY.
    """
    services = get_services()
    if not services.workspace.state.repositories:
        return "Error: no repositories are indexed yet. Call index_repositories first."

    answer, errors = [], []
    # The same stream that powers POST /api/chat; we just join its text events.
    async for event in services.chat.stream(thread_id, question):
        if event.type == "token":
            answer.append(event.data["text"])
        elif event.type == "error":
            errors.append(event.data["message"])
    return "".join(answer) or "Error: " + " ".join(errors)


@mcp.tool
def trace_endpoint(repo: str, method: str, path: str) -> str:
    """Trace an HTTP endpoint from its handler down to the database tables it touches.

    Needs the Neo4j code graph (RA_GRAPH_ENABLED=true).

    Args:
        repo: Name of an indexed repository.
        method: HTTP method, for example "GET".
        path: Route path, for example "/orders/{id}".
    """
    # The chat agent already has a tool with this name when the graph is enabled.
    agent_tool = next((t for t in get_services().chat.tools if t.name == "trace_endpoint"), None)
    if agent_tool is None:
        return "Error: the code graph is disabled. Set RA_GRAPH_ENABLED=true and run Neo4j."
    return str(agent_tool.invoke({"repo": repo, "method": method, "path": path}))


@mcp.tool
async def ask_direct(repo: str, question: str) -> str:
    """Ask a LangChain agent that reads the cloned repository files directly.

    No Ctags JSON index or Neo4j graph is used. `repo` is a git URL, a local path
    (if RA_ALLOW_LOCAL_PATHS is on), or the name of an existing checkout.
    Needs ANTHROPIC_API_KEY.

    Args:
        repo: Git URL, local path, or checkout name.
        question: What to ask about the repository.
    """
    services = get_services()
    try:
        result = await run_direct_agent(
            repo,
            question,
            settings=services.settings,
            fetcher=services.fetcher,
            workspace=services.workspace,
            model=services.model_factory(),
        )
    except AnalyzerError as exc:
        return f"Error: {exc}"
    return result.answer or "Error: the agent returned an empty answer"


@mcp.resource("info://status")
def status() -> dict[str, Any]:
    """Which parts are ready: the LLM key, Universal Ctags and the code graph."""
    services = get_services()
    key = services.settings.anthropic_api_key
    graph = services.graph_store
    return {
        "llm_configured": key is not None and bool(key.get_secret_value()),
        "llm_model": services.settings.llm_model,
        "ctags_available": probe_ctags(services.settings.ctags_path) is not None,
        "graph_available": graph is not None and graph.ping(),
    }


if __name__ == "__main__":
    mcp.run()  # stdio by default
