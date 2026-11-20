"""Indexing pipeline: fetch -> scan -> chunk -> embed, plus the dependency graph.

A run defines the whole workspace: afterwards exactly the requested
repositories are indexed and anything previously indexed but not requested is
removed. The run is not transactional across repositories. If it fails part
way, the workspace keeps describing the previous run while some repositories
may already hold fresh chunks; running it again converges.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from repo_analyzer.config import Settings
from repo_analyzer.domain import Chunk, Manifest
from repo_analyzer.errors import InvalidRepositorySourceError
from repo_analyzer.ingestion.chunker import chunk_file
from repo_analyzer.ingestion.dependency_graph import DependencyGraph
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.manifests import parse_manifest
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import IndexedRepository, Workspace, WorkspaceState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


class IndexingService:
    def __init__(
        self,
        settings: Settings,
        fetcher: RepoFetcher,
        store: VectorStore,
        workspace: Workspace,
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._store = store
        self._workspace = workspace

    def validate(self, sources: Sequence[str]) -> None:
        """Reject a request that cannot succeed, before any cloning starts."""
        if not sources:
            raise InvalidRepositorySourceError("Provide at least one repository")
        if len(sources) > self._settings.max_repositories:
            raise InvalidRepositorySourceError(
                f"At most {self._settings.max_repositories} repositories can be indexed together"
            )
        names = [self._fetcher.resolve_name(source) for source in sources]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise InvalidRepositorySourceError(
                "Repository names must be unique within a workspace; duplicated: "
                + ", ".join(duplicates)
            )

    def run(
        self, sources: Sequence[str], on_progress: ProgressCallback = lambda _: None
    ) -> WorkspaceState:
        self.validate(sources)
        previous = self._workspace.state
        repositories: list[IndexedRepository] = []
        manifests: list[Manifest] = []

        for position, source in enumerate(sources, start=1):
            label = f"[{position}/{len(sources)}]"
            on_progress(f"{label} Fetching {source}")
            snapshot = self._fetcher.fetch(source)

            on_progress(f"{label} Reading {snapshot.name}")
            chunks: list[Chunk] = []
            file_count = 0
            for file in scan_repository(
                snapshot.name, snapshot.root, max_file_bytes=self._settings.max_file_bytes
            ):
                file_count += 1
                chunks.extend(
                    chunk_file(
                        file,
                        max_lines=self._settings.chunk_max_lines,
                        overlap_lines=self._settings.chunk_overlap_lines,
                    )
                )
                if (manifest := parse_manifest(file)) is not None:
                    manifests.append(manifest)

            on_progress(f"{label} Embedding {len(chunks)} chunks from {snapshot.name}")
            self._store.replace_repo(snapshot.name, chunks)
            repositories.append(
                IndexedRepository(
                    name=snapshot.name,
                    source=snapshot.source,
                    root=str(snapshot.root),
                    commit=snapshot.commit,
                    file_count=file_count,
                    chunk_count=len(chunks),
                )
            )

        names = {repository.name for repository in repositories}
        for stale in previous.repositories:
            if stale.name not in names:
                self._store.delete_repo(stale.name)

        state = WorkspaceState(
            repositories=tuple(repositories),
            graph=DependencyGraph.from_manifests(names, manifests),
            indexed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._workspace.replace(state)
        on_progress(f"Indexed {len(repositories)} repositories")
        return state
