"""Core value types shared across ingestion and the agent.

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
    """What a declared dependency is needed for (the ``kind`` in the JSON index)."""

    RUNTIME = "runtime"
    OPTIONAL = "optional"
    DEV = "dev"


@dataclass(frozen=True, slots=True)
class RepoSnapshot:
    """A repository checked out on local disk at a known revision."""

    name: str
    owner: str  # ``"local"`` for a local directory
    source: str
    root: Path
    commit: str | None
    default_branch: str | None


@dataclass(frozen=True, slots=True)
class SourceFile:
    repo: str
    path: str  # POSIX path relative to the repository root
    language: str
    text: str


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
