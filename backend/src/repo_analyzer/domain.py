"""Core value types shared across ingestion, retrieval and the agent.

Everything here is an immutable value object with no I/O, so the rest of the
code base can pass these around freely and tests can build them by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Ecosystem(StrEnum):
    NPM = "npm"
    PYPI = "pypi"
    GO = "go"


class DependencyScope(StrEnum):
    """How strongly a declared dependency binds the dependent."""

    RUNTIME = "runtime"
    OPTIONAL = "optional"
    DEV = "dev"


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    """A repository checked out on local disk at a known revision."""

    name: str
    source: str
    root: Path
    commit: str | None


@dataclass(frozen=True, slots=True)
class SourceFile:
    repo: str
    path: str  # POSIX path relative to the repository root
    language: str
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    repo: str
    path: str
    language: str
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    text: str

    @property
    def id(self) -> str:
        return f"{self.repo}:{self.path}:{self.start_line}-{self.end_line}"

    @property
    def citation(self) -> str:
        return f"{self.repo}/{self.path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True, slots=True)
class PackageRef:
    ecosystem: Ecosystem
    name: str  # normalised for the ecosystem; see ingestion.manifests


@dataclass(frozen=True, slots=True)
class Requirement:
    package: PackageRef
    scope: DependencyScope


@dataclass(frozen=True, slots=True)
class Manifest:
    """A package manifest found in a repository."""

    repo: str
    path: str
    provides: PackageRef | None
    requires: tuple[Requirement, ...]


@dataclass(frozen=True, slots=True)
class DependencyEdge:
    """``source_repo`` declares a dependency on a package published by ``target_repo``."""

    source_repo: str
    target_repo: str
    package: PackageRef
    scope: DependencyScope
    manifest_path: str  # where in ``source_repo`` the dependency is declared
