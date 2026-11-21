"""The set of indexed repositories and their dependency graph.

``WorkspaceState`` is an immutable snapshot. ``Workspace`` holds the current
snapshot, swaps it atomically when an indexing run finishes, and persists it as
JSON so a restart does not require re-indexing. Readers (HTTP handlers, agent
tools) take a reference to the snapshot once and work from that, so they never
see a half-updated view.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from repo_analyzer.errors import UnknownRepositoryError
from repo_analyzer.ingestion.dependency_graph import DependencyGraph

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class IndexedRepository:
    name: str
    source: str
    root: str
    commit: str | None
    file_count: int
    chunk_count: int = 0  # rag mode
    symbol_count: int = 0  # json mode


@dataclass(frozen=True, slots=True)
class WorkspaceState:
    repositories: tuple[IndexedRepository, ...] = ()
    graph: DependencyGraph = field(default_factory=lambda: DependencyGraph((), ()))
    indexed_at: str | None = None  # ISO 8601, UTC
    knowledge_mode: str = "rag"  # which artifacts this index run produced

    def repository(self, name: str) -> IndexedRepository:
        for repository in self.repositories:
            if repository.name == name:
                return repository
        known = ", ".join(r.name for r in self.repositories) or "none"
        raise UnknownRepositoryError(f"Unknown repository {name!r} (indexed: {known})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "indexed_at": self.indexed_at,
            "knowledge_mode": self.knowledge_mode,
            "repositories": [asdict(r) for r in self.repositories],
            "graph": self.graph.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceState:
        return cls(
            repositories=tuple(IndexedRepository(**r) for r in data["repositories"]),
            graph=DependencyGraph.from_dict(data["graph"]),
            indexed_at=data.get("indexed_at"),
            # Files written before the mode existed were always vector-store indexes.
            knowledge_mode=data.get("knowledge_mode", "rag"),
        )


class Workspace:
    def __init__(self, state_file: Path) -> None:
        self._state_file = state_file
        self._lock = threading.Lock()
        self._state = self._load()

    @property
    def state(self) -> WorkspaceState:
        with self._lock:
            return self._state

    def replace(self, state: WorkspaceState) -> None:
        with self._lock:
            self._persist(state)
            self._state = state

    def _load(self) -> WorkspaceState:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return WorkspaceState()
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable workspace file %s: %s", self._state_file, exc)
            return WorkspaceState()
        if data.get("schema_version") != _SCHEMA_VERSION:
            logger.warning("Ignoring workspace file with an unknown schema version")
            return WorkspaceState()
        try:
            return WorkspaceState.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Ignoring malformed workspace file %s: %s", self._state_file, exc)
            return WorkspaceState()

    def _persist(self, state: WorkspaceState) -> None:
        # Write-then-rename so a crash mid-write cannot leave a truncated file.
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_file.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
        os.replace(temporary, self._state_file)
