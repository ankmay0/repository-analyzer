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
    requested_diagram,
)
from repo_analyzer.agent.service import ChatService
from repo_analyzer.config import Settings
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.workspace import Workspace, WorkspaceState
from support import ScriptedChatModel


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


def test_requested_diagram_fires_only_on_a_draw_request(json_workspace: Workspace) -> None:
    state = json_workspace.state

    assert requested_diagram(state, "How do these repositories depend on each other?") is None
    assert requested_diagram(state, "Which repository is safest to change?") is None

    drawn = requested_diagram(state, "Draw how these repositories depend on each other.")
    assert drawn is not None and drawn.title.startswith("Repository dependencies")
    assert {n.status for n in drawn.nodes} == {"normal"}

    for phrasing in ["visualise the dependencies", "show me a diagram", "map out the repos"]:
        assert requested_diagram(state, phrasing) is not None, phrasing


def test_requested_diagram_highlights_impact_only_with_a_failure_cue(
    json_workspace: Workspace,
) -> None:
    state = json_workspace.state

    failing = requested_diagram(state, "Draw what breaks if core-lib goes down.")
    assert failing is not None
    status = {n.id: n.status for n in failing.nodes}
    assert status["core-lib"] == "failing" and status["mid-lib"] == "affected"

    # A draw request that names a repository without a failure cue stays the plain graph.
    plain = requested_diagram(state, "Draw the graph around mid-lib.")
    assert plain is not None and all(n.status == "normal" for n in plain.nodes)


def test_requested_diagram_is_silent_when_nothing_is_indexed() -> None:
    assert requested_diagram(WorkspaceState(), "draw the dependencies") is None


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


async def test_a_draw_request_emits_the_dependency_diagram_before_the_answer(
    json_settings: Settings, json_workspace: Workspace, knowledge: KnowledgeStore
) -> None:
    # The model only answers in words; the diagram comes from the data, not a tool call.
    hub = ScriptedChatModel(responses=[AIMessage(content="core-lib's dependents are affected.")])
    service = ChatService(
        lambda role: hub, json_settings, json_workspace, knowledge, None, prompt_caching=False
    )

    events = [event async for event in service.stream("t1", "Draw what breaks if core-lib fails")]

    assert [e.type for e in events] == ["diagram", "token", "token", "done"]
    diagram = events[0].data
    assert diagram["title"].startswith("What is affected if core-lib fails")
    assert {n["id"]: n["status"] for n in diagram["nodes"]}["core-lib"] == "failing"
    assert len(diagram["edges"]) == 4
    # No tool was needed, and the model was never asked to draw.
    assert all(not m.tool_calls for call in hub.calls for m in call if isinstance(m, AIMessage))


async def test_an_ordinary_question_emits_no_diagram(
    json_settings: Settings, json_workspace: Workspace, knowledge: KnowledgeStore
) -> None:
    hub = ScriptedChatModel(responses=[AIMessage(content="mid-lib and web-app depend on it.")])
    service = ChatService(
        lambda role: hub, json_settings, json_workspace, knowledge, None, prompt_caching=False
    )

    events = [event async for event in service.stream("t1", "What is affected if core-lib fails?")]

    assert not [e for e in events if e.type == "diagram"]
