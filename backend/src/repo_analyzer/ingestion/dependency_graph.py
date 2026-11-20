"""Cross-repository dependency graph and impact ("blast radius") analysis.

Nodes are repositories. An edge ``A -> B`` means a manifest in ``A`` declares a
dependency on a package that a manifest in ``B`` publishes. The impact of a
repository failing is everything that can reach it by following edges, i.e.
its transitive dependents.

The graph is small (a handful of repositories), so it is stored as a plain
edge list and traversed with breadth-first search; no graph library is needed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from repo_analyzer.domain import (
    DependencyEdge,
    DependencyScope,
    Ecosystem,
    Manifest,
    PackageRef,
)
from repo_analyzer.errors import UnknownRepositoryError


@dataclass(frozen=True, slots=True)
class ImpactedRepository:
    """A repository affected when the analysed repository fails."""

    repo: str
    distance: int  # number of dependency hops from the failing repository
    # Edges from this repository down to the failing one, nearest dependency first.
    path: tuple[DependencyEdge, ...]
    # True when at least one edge on the path is optional or development-only,
    # i.e. the repository may keep working at runtime.
    weak: bool


class DependencyGraph:
    def __init__(self, repos: Iterable[str], edges: Iterable[DependencyEdge]) -> None:
        self._repos = tuple(sorted(set(repos)))
        # Shallower manifests sort first, so when several manifests declare the same
        # dependency the repository's root manifest is the one reported as evidence.
        self._edges = tuple(
            sorted(
                set(edges),
                key=lambda e: (
                    e.source_repo,
                    e.target_repo,
                    e.manifest_path.count("/"),
                    e.manifest_path,
                    e.package.name,
                    e.scope,
                ),
            )
        )

    @classmethod
    def from_manifests(cls, repos: Iterable[str], manifests: Iterable[Manifest]) -> DependencyGraph:
        manifests = list(manifests)
        # If two repositories claim the same package name (forks, vendored copies,
        # example projects), the first in sorted order wins so results are stable.
        publisher: dict[PackageRef, str] = {}
        for manifest in sorted(manifests, key=lambda m: (m.repo, m.path)):
            if manifest.provides is not None:
                publisher.setdefault(manifest.provides, manifest.repo)

        edges = [
            DependencyEdge(
                source_repo=manifest.repo,
                target_repo=target,
                package=requirement.package,
                scope=requirement.scope,
                manifest_path=manifest.path,
            )
            for manifest in manifests
            for requirement in manifest.requires
            if (target := publisher.get(requirement.package)) is not None
            and target != manifest.repo
        ]
        return cls(repos, edges)

    @property
    def repos(self) -> tuple[str, ...]:
        return self._repos

    @property
    def edges(self) -> tuple[DependencyEdge, ...]:
        return self._edges

    def dependencies_of(self, repo: str) -> tuple[DependencyEdge, ...]:
        self._require(repo)
        return tuple(e for e in self._edges if e.source_repo == repo)

    def dependents_of(self, repo: str) -> tuple[DependencyEdge, ...]:
        self._require(repo)
        return tuple(e for e in self._edges if e.target_repo == repo)

    def impact_of(self, repo: str) -> tuple[ImpactedRepository, ...]:
        """Return every repository that transitively depends on ``repo``.

        Runtime-only paths are searched first, so a repository reachable both
        through a runtime chain and a dev-only chain is reported as a strong
        (``weak=False``) impact with its runtime path. Cycles are handled: each
        repository is reported once, and the failing repository never is.
        """
        self._require(repo)
        strong = self._reverse_bfs(repo, runtime_only=True)
        everything = self._reverse_bfs(repo, runtime_only=False)
        impacted = [
            ImpactedRepository(repo=name, distance=len(path), path=path, weak=False)
            for name, path in strong.items()
        ]
        impacted += [
            ImpactedRepository(repo=name, distance=len(path), path=path, weak=True)
            for name, path in everything.items()
            if name not in strong
        ]
        return tuple(sorted(impacted, key=lambda i: (i.weak, i.distance, i.repo)))

    # -- persistence -----------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos": list(self._repos),
            "edges": [
                {
                    "source_repo": e.source_repo,
                    "target_repo": e.target_repo,
                    "ecosystem": e.package.ecosystem.value,
                    "package": e.package.name,
                    "scope": e.scope.value,
                    "manifest_path": e.manifest_path,
                }
                for e in self._edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DependencyGraph:
        edges = [
            DependencyEdge(
                source_repo=e["source_repo"],
                target_repo=e["target_repo"],
                package=PackageRef(Ecosystem(e["ecosystem"]), e["package"]),
                scope=DependencyScope(e["scope"]),
                manifest_path=e["manifest_path"],
            )
            for e in data.get("edges", [])
        ]
        return cls(data.get("repos", []), edges)

    # -- internals -------------------------------------------------------------

    def _require(self, repo: str) -> None:
        if repo not in self._repos:
            known = ", ".join(self._repos) or "none"
            raise UnknownRepositoryError(f"Unknown repository {repo!r} (indexed: {known})")

    def _reverse_bfs(
        self, origin: str, *, runtime_only: bool
    ) -> dict[str, tuple[DependencyEdge, ...]]:
        """Shortest dependency path from each dependent down to ``origin``."""
        incoming: dict[str, list[DependencyEdge]] = defaultdict(list)
        for edge in self._edges:
            if runtime_only and edge.scope is not DependencyScope.RUNTIME:
                continue
            incoming[edge.target_repo].append(edge)

        paths: dict[str, tuple[DependencyEdge, ...]] = {}
        queue: deque[tuple[str, tuple[DependencyEdge, ...]]] = deque([(origin, ())])
        while queue:
            current, path_to_origin = queue.popleft()
            for edge in incoming[current]:
                dependent = edge.source_repo
                if dependent == origin or dependent in paths:
                    continue
                paths[dependent] = (edge, *path_to_origin)
                queue.append((dependent, paths[dependent]))
        return paths
