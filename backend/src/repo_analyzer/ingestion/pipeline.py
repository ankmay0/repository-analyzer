"""Indexing pipeline: fetch -> scan -> describe, writing one JSON file per repository.

A run defines the whole workspace: afterwards exactly the requested repositories
are indexed and anything previously indexed but not requested is removed. The
JSON files are written only after every repository has been fetched and
scanned, so a run that fails while fetching leaves the previous indexes and
workspace untouched (the checkouts of repositories fetched before the failure
may already be refreshed). Running it again converges.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from repo_analyzer.config import Settings
from repo_analyzer.errors import InvalidRepositorySourceError
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.graph_extract import extract_graph
from repo_analyzer.ingestion.graph_store import GraphStore
from repo_analyzer.ingestion.metadata import MetadataStore
from repo_analyzer.ingestion.repo_index import (
    RepoIndex,
    RepoIndexStore,
    build_repo_index,
    index_filename,
    symbol_count,
)
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.ingestion.symbols import require_ctags
from repo_analyzer.workspace import IndexedRepository, Workspace, WorkspaceState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]
# Builds endpoint metadata for one repository from its index and checkout root.
MetadataHook = Callable[[RepoIndex, Path], dict[str, Any]]


class IndexingService:
    def __init__(
        self,
        settings: Settings,
        fetcher: RepoFetcher,
        index_store: RepoIndexStore,
        workspace: Workspace,
        *,
        metadata_hook: MetadataHook | None = None,
        metadata_store: MetadataStore | None = None,
        graph_store: GraphStore | None = None,
        graph_ws: str = "",
    ) -> None:
        self._settings = settings
        self._fetcher = fetcher
        self._index_store = index_store
        self._workspace = workspace
        self._metadata_hook = metadata_hook
        self._metadata_store = metadata_store
        self._graph_store = graph_store
        self._graph_ws = graph_ws

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
        require_ctags(self._settings.ctags_path)

    def run(
        self, sources: Sequence[str], on_progress: ProgressCallback = lambda _: None
    ) -> WorkspaceState:
        self.validate(sources)
        previous = self._workspace.state
        indexed_at = datetime.now(UTC).isoformat(timespec="seconds")
        described: list[tuple[IndexedRepository, RepoIndex]] = []

        for position, source in enumerate(sources, start=1):
            label = f"[{position}/{len(sources)}]"
            on_progress(f"{label} Fetching {source}")
            snapshot = self._fetcher.fetch(source)

            on_progress(f"{label} Describing {snapshot.name}")
            index = build_repo_index(
                snapshot,
                scan_repository(
                    snapshot.name, snapshot.root, max_file_bytes=self._settings.max_file_bytes
                ),
                indexed_at=indexed_at,
                ctags_path=self._settings.ctags_path,
            )
            repository = IndexedRepository(
                name=snapshot.name,
                owner=snapshot.owner,
                source=snapshot.source,
                root=str(snapshot.root),
                commit=snapshot.commit,
                default_branch=snapshot.default_branch,
                file_count=index["stats"]["files"],
                symbol_count=symbol_count(index),
                dependency_count=len(index["dependencies"]),
                index_file=index_filename(snapshot.owner, snapshot.name),
            )
            described.append((repository, index))

        on_progress("Writing JSON indexes")
        for _, index in described:
            self._index_store.save(index)
        self._write_metadata(described, on_progress)

        repositories = tuple(repository for repository, _ in described)
        state = WorkspaceState(repositories=repositories, indexed_at=indexed_at)
        self._workspace.replace(state)

        kept = {repository.index_file for repository in repositories}
        for stale in previous.repositories:
            if stale.index_file not in kept:
                self._index_store.delete(stale.index_file)
                if self._metadata_store is not None:
                    self._metadata_store.delete(stale.owner, stale.name)
        self._sync_graph(repositories, previous, on_progress)

        on_progress(f"Indexed {len(repositories)} repositories")
        return state

    def _sync_graph(
        self,
        repositories: tuple[IndexedRepository, ...],
        previous: WorkspaceState,
        on_progress: ProgressCallback,
    ) -> None:
        """Rebuild each repository's code graph. A failure never fails indexing."""
        store = self._graph_store
        if store is None:
            return
        on_progress("Building code graph")
        kept = {repository.name for repository in repositories}
        for stale in previous.repositories:
            if stale.name not in kept:
                try:
                    store.delete_repo(self._graph_ws, stale.name)
                except Exception as exc:  # Neo4j down, driver missing, ...
                    logger.warning(
                        "Removing %s from the code graph failed: %s: %s",
                        stale.name,
                        type(exc).__name__,
                        exc,
                        exc_info=True,
                    )
        synced: list[str] = []
        for repository in repositories:
            try:
                records = extract_graph(
                    scan_repository(
                        repository.name,
                        Path(repository.root),
                        max_file_bytes=self._settings.max_file_bytes,
                    )
                )
                logger.debug(
                    "Code graph for %s: %d functions, %d calls, %d endpoints, %d tables",
                    repository.name,
                    len(records.functions),
                    len(records.calls),
                    len(records.endpoints),
                    len(records.tables),
                )
                store.sync_repo(self._graph_ws, repository.name, repository.commit, records)
                synced.append(repository.name)
            except Exception as exc:  # the JSON index is already written: keep it
                logger.warning(
                    "Code graph for %s failed: %s: %s",
                    repository.name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
        logger.info(
            "Code graph: %d of %d repositories synced (ws=%s)%s",
            len(synced),
            len(repositories),
            self._graph_ws,
            "" if len(synced) == len(repositories) else "; see the warnings above",
        )

    def _write_metadata(
        self,
        described: list[tuple[IndexedRepository, RepoIndex]],
        on_progress: ProgressCallback,
    ) -> None:
        """Generate endpoint metadata if configured. A failure never fails indexing."""
        if self._metadata_hook is None or self._metadata_store is None:
            return
        for repository, index in described:
            on_progress(f"Describing endpoints in {repository.name}")
            try:
                metadata = self._metadata_hook(index, Path(repository.root))
            except Exception as exc:  # model, network or parse failure: keep the index
                logger.warning("Endpoint metadata for %s failed: %s", repository.name, exc)
                self._metadata_store.delete(repository.owner, repository.name)
                continue
            self._metadata_store.save(metadata)
