"""Diagrams: the data the hub sends for the UI to draw."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool

from repo_analyzer.agent.diagrams import (
    MAX_NODES,
    Diagram,
    DiagramEdge,
    DiagramNode,
    build_diagram_tools,
    dependency_diagram,
)
from repo_analyzer.agent.service import ChatService
from repo_analyzer.config import Settings
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.workspace import Workspace
from support import ScriptedChatModel, tool_call


@pytest.fixture
def shown() -> list[dict[str, Any]]:
    return []


@pytest.fixture
def tools(json_workspace: Workspace, shown: list[dict[str, Any]]) -> dict[str, BaseTool]:
    return {t.name: t for t in build_diagram_tools(json_workspace, shown.append)}


def test_dependency_diagram_has_one_arrow_per_pair_dashed_when_not_runtime(
    json_workspace: Workspace,
) -> None:
    diagram = dependency_diagram(json_workspace.state)

    assert diagram.title.startswith("Repository dependencies")
    assert [(n.id, n.kind, n.status) for n in diagram.nodes] == [
        ("core-lib", "repository", "normal"),
        ("mid-lib", "repository", "normal"),
        ("tools", "repository", "normal"),
        ("web-app", "repository", "normal"),
    ]
    assert {(e.source, e.target, e.dashed) for e in diagram.edges} == {
        ("mid-lib", "core-lib", False),
        ("web-app", "mid-lib", False),
        ("web-app", "core-lib", True),  # development-only
        ("tools", "web-app", True),
    }
    assert diagram.problems() is None


def test_dependency_diagram_marks_the_failing_repository_and_what_it_affects(
    json_workspace: Workspace,
) -> None:
    diagram = dependency_diagram(json_workspace.state, failing="core-lib")

    assert diagram.title.startswith("What is affected if core-lib fails")
    assert {n.id: n.status for n in diagram.nodes} == {
        "core-lib": "failing",
        "mid-lib": "affected",
        "web-app": "affected",
        "tools": "weak",
    }
    untouched = dependency_diagram(json_workspace.state, failing="tools")
    assert {n.id: n.status for n in untouched.nodes}["core-lib"] == "normal"


async def test_show_dependency_graph_publishes_the_diagram_and_tells_the_model(
    tools: dict[str, BaseTool], shown: list[dict[str, Any]]
) -> None:
    reply = await tools["show_dependency_graph"].ainvoke({"failing_repo": "core-lib"})

    assert reply.startswith("Shown to the user as an interactive diagram")
    assert "(4 nodes, 4 arrows)" in reply
    (event,) = shown
    assert event["type"] == "diagram"
    assert event["diagram"]["title"].startswith("What is affected if core-lib fails")
    assert {"id": "core-lib", "label": "core-lib", "kind": "repository", "status": "failing"} in (
        event["diagram"]["nodes"]
    )

    error = await tools["show_dependency_graph"].ainvoke({"failing_repo": "nope"})
    assert error.startswith("Error: Unknown repository 'nope'") and len(shown) == 1


async def test_show_graph_accepts_model_supplied_nodes_and_edges(
    tools: dict[str, BaseTool], shown: list[dict[str, Any]]
) -> None:
    reply = await tools["show_graph"].ainvoke(
        {
            "title": "Who uses sign_token",
            "nodes": [
                {"id": "def", "label": "sign_token", "kind": "symbol"},
                {"id": "mid", "label": "mid_lib/session.py", "kind": "file"},
            ],
            "edges": [{"source": "mid", "target": "def", "label": "imports"}],
        }
    )

    assert "'Who uses sign_token' (2 nodes, 1 arrows)" in reply
    assert shown[0]["diagram"]["edges"] == [
        {"source": "mid", "target": "def", "label": "imports", "dashed": False}
    ]
    assert shown[0]["diagram"]["nodes"][1] == {
        "id": "mid", "label": "mid_lib/session.py", "kind": "file", "status": "normal",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("nodes", "edges", "problem"),
    [
        ([], [], "at least one node"),
        ([("a", "A"), ("a", "again")], [], "repeated: a"),
        ([("a", "A")], [("a", "ghost")], "not in nodes: ghost"),
        ([(f"n{i}", "x") for i in range(MAX_NODES + 1)], [], "too large to read"),
    ],
)
async def test_show_graph_rejects_diagrams_that_cannot_be_drawn(
    tools: dict[str, BaseTool],
    shown: list[dict[str, Any]],
    nodes: list[tuple[str, str]],
    edges: list[tuple[str, str]],
    problem: str,
) -> None:
    diagram = Diagram(
        title="t",
        nodes=[DiagramNode(id=i, label=label) for i, label in nodes],
        edges=[DiagramEdge(source=s, target=t) for s, t in edges],
    )
    assert problem in (diagram.problems() or "")

    reply = await tools["show_graph"].ainvoke(diagram.model_dump())

    assert reply.startswith("Error:") and problem in reply
    assert shown == []  # nothing half-drawn reaches the user


async def test_a_diagram_requested_by_the_hub_reaches_the_client_as_an_event(
    json_settings: Settings, json_workspace: Workspace, knowledge: KnowledgeStore
) -> None:
    hub = ScriptedChatModel(
        responses=[
            tool_call("show_dependency_graph", {"failing_repo": "core-lib"}),
            AIMessage(content="Three repositories are affected; see the diagram."),
        ]
    )
    service = ChatService(
        lambda role: hub, json_settings, json_workspace, knowledge, None, prompt_caching=False
    )

    events = [event async for event in service.stream("t1", "Draw what breaks if core-lib fails")]

    assert [e.type for e in events] == ["tool_call", "diagram", "token", "token", "done"]
    diagram = events[1].data
    assert diagram["title"].startswith("What is affected if core-lib fails")
    assert len(diagram["nodes"]) == 4 and len(diagram["edges"]) == 4
    # The model is told the diagram was shown, not handed its contents to repeat.
    assert "Shown to the user as an interactive diagram" in str(hub.calls[-1][-1].content)
