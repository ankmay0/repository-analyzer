from __future__ import annotations

import pytest

from repo_analyzer.domain import (
    DependencyEdge,
    DependencyScope,
    Ecosystem,
    Manifest,
    PackageRef,
    Requirement,
)
from repo_analyzer.errors import UnknownRepositoryError
from repo_analyzer.ingestion.dependency_graph import DependencyGraph


def edge(
    source: str, target: str, scope: DependencyScope = DependencyScope.RUNTIME
) -> DependencyEdge:
    return DependencyEdge(
        source_repo=source,
        target_repo=target,
        package=PackageRef(Ecosystem.PYPI, target),
        scope=scope,
        manifest_path="pyproject.toml",
    )


def manifest(repo: str, provides: str | None, *requires: str) -> Manifest:
    return Manifest(
        repo=repo,
        path="pyproject.toml",
        provides=PackageRef(Ecosystem.PYPI, provides) if provides else None,
        requires=tuple(
            Requirement(PackageRef(Ecosystem.PYPI, name), DependencyScope.RUNTIME)
            for name in requires
        ),
    )


def impact(graph: DependencyGraph, repo: str) -> dict[str, tuple[int, bool]]:
    return {i.repo: (i.distance, i.weak) for i in graph.impact_of(repo)}


def test_edges_link_requirements_to_the_repository_that_publishes_the_package() -> None:
    graph = DependencyGraph.from_manifests(
        ["a", "b", "c"],
        [
            manifest("a", "pkg-a"),
            manifest("b", "pkg-b", "pkg-a", "requests"),  # requests is external: no edge
            manifest("c", None, "pkg-b", "pkg-a"),
            manifest("a", "pkg-a-examples", "pkg-a"),  # a repo depending on itself: no edge
        ],
    )
    assert {(e.source_repo, e.target_repo) for e in graph.edges} == {
        ("b", "a"),
        ("c", "a"),
        ("c", "b"),
    }


def test_impact_is_transitive_and_reports_the_dependency_chain() -> None:
    graph = DependencyGraph(["a", "b", "c", "d"], [edge("b", "a"), edge("c", "b")])

    assert impact(graph, "a") == {"b": (1, False), "c": (2, False)}
    assert impact(graph, "c") == {}
    assert impact(graph, "d") == {}

    c = next(i for i in graph.impact_of("a") if i.repo == "c")
    assert [(e.source_repo, e.target_repo) for e in c.path] == [("c", "b"), ("b", "a")]


def test_mutually_dependent_repositories_do_not_loop() -> None:
    graph = DependencyGraph(["a", "b", "c"], [edge("a", "b"), edge("b", "c"), edge("c", "a")])

    assert impact(graph, "a") == {"c": (1, False), "b": (2, False)}
    assert impact(graph, "b") == {"a": (1, False), "c": (2, False)}


def test_paths_through_dev_or_optional_dependencies_are_weak() -> None:
    graph = DependencyGraph(
        ["a", "b", "c", "d"],
        [
            edge("b", "a"),
            edge("c", "b", DependencyScope.DEV),
            edge("d", "a", DependencyScope.OPTIONAL),
        ],
    )
    assert impact(graph, "a") == {"b": (1, False), "c": (2, True), "d": (1, True)}


def test_a_runtime_path_wins_over_a_shorter_dev_path() -> None:
    graph = DependencyGraph(
        ["a", "b", "c"],
        [edge("c", "a", DependencyScope.DEV), edge("c", "b"), edge("b", "a")],
    )
    assert impact(graph, "a") == {"b": (1, False), "c": (2, False)}


def test_unknown_repository_is_an_error() -> None:
    graph = DependencyGraph(["a"], [])
    with pytest.raises(UnknownRepositoryError, match="indexed: a"):
        graph.impact_of("nope")


def test_round_trips_through_its_persisted_form() -> None:
    graph = DependencyGraph(["a", "b"], [edge("b", "a", DependencyScope.DEV)])
    restored = DependencyGraph.from_dict(graph.to_dict())

    assert restored.repos == graph.repos
    assert restored.edges == graph.edges


def test_the_shallowest_manifest_is_reported_as_evidence() -> None:
    nested = DependencyEdge(
        "b", "a", PackageRef(Ecosystem.PYPI, "a"), DependencyScope.RUNTIME, "a/b/requirements.txt"
    )
    root = DependencyEdge(
        "b", "a", PackageRef(Ecosystem.PYPI, "a"), DependencyScope.RUNTIME, "pyproject.toml"
    )
    graph = DependencyGraph(["a", "b"], [nested, root])

    assert graph.impact_of("a")[0].path == (root,)
