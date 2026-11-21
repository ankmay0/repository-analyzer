"""Diagrams the hub can show to the user.

A diagram is plain data: nodes and edges with a little styling intent. The hub
calls a tool, the tool publishes the diagram on the response stream, and the UI
draws it as an interactive graph. Nothing about rendering or layout lives here.

Two tools are offered:

* ``show_dependency_graph`` builds its diagram from the manifest-derived
  dependency graph, so what it shows is exact.
* ``show_graph`` draws whatever nodes and edges the model supplies, for anything
  else worth picturing. Its content is only as reliable as the model's sources,
  which is why the hub's prompt restricts it to relationships its tools reported.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool
from langgraph.config import get_stream_writer
from pydantic import BaseModel, Field

from repo_analyzer.errors import AnalyzerError
from repo_analyzer.workspace import Workspace, WorkspaceState

DIAGRAM_EVENT = "diagram"
MAX_NODES = 60
MAX_EDGES = 150

Emit = Callable[[dict[str, Any]], None]


class DiagramNode(BaseModel):
    id: str = Field(min_length=1, max_length=160, description="Unique within the diagram.")
    label: str = Field(min_length=1, max_length=80, description="Text shown on the node.")
    kind: Literal["repository", "file", "symbol", "external", "step", "other"] = Field(
        default="other", description="What the node represents; decides its colour."
    )
    status: Literal["normal", "failing", "affected", "weak"] = Field(
        default="normal",
        description=(
            "Use for impact pictures: 'failing' for the thing that breaks, 'affected' for "
            "what breaks with it, 'weak' for what is affected only through an optional or "
            "development dependency."
        ),
    )


class DiagramEdge(BaseModel):
    source: str = Field(description="id of the node the arrow starts at.")
    target: str = Field(description="id of the node the arrow points to.")
    label: str = Field(default="", max_length=60, description="Short verb, e.g. 'imports'.")
    dashed: bool = Field(default=False, description="Draw dashed, for weak or optional links.")


class Diagram(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    nodes: list[DiagramNode]
    edges: list[DiagramEdge]

    def problems(self) -> str | None:
        """Why this diagram cannot be drawn, or ``None`` if it can."""
        if not self.nodes:
            return "a diagram needs at least one node"
        if len(self.nodes) > MAX_NODES or len(self.edges) > MAX_EDGES:
            return (
                f"too large to read ({len(self.nodes)} nodes, {len(self.edges)} edges; the "
                f"limits are {MAX_NODES} and {MAX_EDGES}). Show a narrower slice"
            )
        ids = [node.id for node in self.nodes]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            return f"node ids must be unique; repeated: {', '.join(duplicates)}"
        known = set(ids)
        dangling = sorted(
            {end for edge in self.edges for end in (edge.source, edge.target) if end not in known}
        )
        if dangling:
            return f"edges refer to node ids that are not in nodes: {', '.join(dangling)}"
        return None


def dependency_diagram(state: WorkspaceState, failing: str | None = None) -> Diagram:
    """The repository dependency graph, optionally marked up with the impact of a failure."""
    status: dict[str, Literal["normal", "failing", "affected", "weak"]] = {}
    if failing is not None:
        status[failing] = "failing"
        for item in state.graph.impact_of(failing):
            status[item.repo] = "weak" if item.weak else "affected"

    # One arrow per pair of repositories; solid if any manifest makes it a runtime dependency.
    runtime: dict[tuple[str, str], bool] = {}
    for edge in state.graph.edges:
        key = (edge.source_repo, edge.target_repo)
        runtime[key] = runtime.get(key, False) or edge.scope.value == "runtime"

    title = (
        f"What is affected if {failing} fails (arrows point to dependencies)"
        if failing
        else "Repository dependencies (arrows point to dependencies)"
    )
    return Diagram(
        title=title,
        nodes=[
            DiagramNode(id=name, label=name, kind="repository", status=status.get(name, "normal"))
            for name in state.graph.repos
        ],
        edges=[
            # Unlabelled: every arrow here means "depends on", which the title conveys.
            DiagramEdge(source=source, target=target, dashed=not is_runtime)
            for (source, target), is_runtime in sorted(runtime.items())
        ],
    )


def build_diagram_tools(workspace: Workspace, emit: Emit | None = None) -> list[BaseTool]:
    """Tools that show a diagram to the user.

    ``emit`` receives each diagram; by default it is the response stream of the
    graph the tool is running in.
    """

    def publish(diagram: Diagram) -> str:
        (emit or get_stream_writer())({"type": DIAGRAM_EVENT, "diagram": diagram.model_dump()})
        return (
            f"Shown to the user as an interactive diagram: {diagram.title!r} "
            f"({len(diagram.nodes)} nodes, {len(diagram.edges)} arrows). "
            "Do not redraw it in text; refer to it and explain what it shows."
        )

    @tool
    async def show_dependency_graph(failing_repo: str | None = None) -> str:
        """Show the user a diagram of how the indexed repositories depend on each other.

        The diagram is built from package manifests, so it is exact. Arrows point from a
        repository to one it depends on.

        Args:
            failing_repo: If given, highlight this repository as failing and mark every
                repository that would be affected, as analyze_impact reports them.
        """
        try:
            diagram = dependency_diagram(workspace.state, failing_repo)
        except AnalyzerError as exc:
            return f"Error: {exc}"
        if not diagram.nodes:
            return "No repositories are indexed yet."
        return publish(diagram)

    @tool
    async def show_graph(title: str, nodes: list[DiagramNode], edges: list[DiagramEdge]) -> str:
        """Show the user a diagram of any set of things and the relationships between them.

        Use it for pictures other than the repository dependency graph: which files use a
        function, how modules import each other, the steps of a request. Every node and
        arrow must come from what your tools and the experts reported.

        Args:
            title: What the diagram shows, as a short heading.
            nodes: The boxes. Keep labels short; put file paths or names, not sentences.
            edges: The arrows, each from one node id to another.
        """
        diagram = Diagram(title=title, nodes=nodes, edges=edges)
        problem = diagram.problems()
        if problem is not None:
            return f"Error: {problem}."
        return publish(diagram)

    return [show_dependency_graph, show_graph]
