"""Indexing pipeline: fetch and scan each repository, then build what agents read.

What gets built depends on ``Settings.knowledge_mode``:

* ``json``: the pack, map and graph files (see ``repo_analyzer.knowledge``).
* ``rag``: chunks embedded into the vector store.

The repository-level dependency graph from package manifests is built in both.

A run defines the whole workspace: afterwards exactly the requested
repositories are indexed and anything previously indexed but not requested is
removed. The run is not transactional across repositories. If it fails part
way, the workspace keeps describing the previous run while some repositories
may already hold fresh data; running it again converges.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from repo_analyzer.config import Settings
from repo_analyzer.domain import Chunk, Manifest, RepoSnapshot, SourceFile
from repo_analyzer.errors import InvalidRepositorySourceError
from repo_analyzer.ingestion.chunker import chunk_file
from repo_analyzer.ingestion.dependency_graph import DependencyGraph
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.manifests import parse_manifest
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.knowledge.builder import build_map, build_pack, find_package_dirs
from repo_analyzer.knowledge.graph import build_graph
from repo_analyzer.knowledge.outline import FileOutline, outline_file
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.retrieval.store import VectorStore
from repo_analyzer.workspace import IndexedRepository, Workspace, WorkspaceState

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


class IndexingService:
    def __init__(
        self,
        settings: Settings,
        fetcher: RepoFetcher,
        workspace: Workspace,
        knowledge: KnowledgeStore,
        store: VectorStore | None,
    ) -> None:
        if settings.knowledge_mode == "rag" and store is None:
            raise ValueError("rag mode requires a vector store")
        self._settings = settings
        self._fetcher = fetcher
        self._workspace = workspace
        self._knowledge = knowledge
        self._store = store

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
        outlines: dict[str, list[FileOutline]] = {}
        commits: dict[str, str | None] = {}

        for position, source in enumerate(sources, start=1):
            label = f"[{position}/{len(sources)}]"
            on_progress(f"{label} Fetching {source}")
            snapshot = self._fetcher.fetch(source)

            on_progress(f"{label} Reading {snapshot.name}")
            files = list(
                scan_repository(
                    snapshot.name, snapshot.root, max_file_bytes=self._settings.max_file_bytes
                )
            )
            manifests += [m for file in files if (m := parse_manifest(file)) is not None]
            commits[snapshot.name] = snapshot.commit

            if self._store is None:
                on_progress(f"{label} Mapping {len(files)} files from {snapshot.name}")
                outlines[snapshot.name] = self._write_knowledge(snapshot, files)
                symbol_count = sum(len(o.symbols) for o in outlines[snapshot.name])
                chunk_count = 0
            else:
                chunks = self._chunk(files)
                on_progress(f"{label} Embedding {len(chunks)} chunks from {snapshot.name}")
                self._store.replace_repo(snapshot.name, chunks)
                symbol_count, chunk_count = 0, len(chunks)

            repositories.append(
                IndexedRepository(
                    name=snapshot.name,
                    source=snapshot.source,
                    root=str(snapshot.root),
                    commit=snapshot.commit,
                    file_count=len(files),
                    chunk_count=chunk_count,
                    symbol_count=symbol_count,
                )
            )

        names = {repository.name for repository in repositories}
        dependencies = DependencyGraph.from_manifests(names, manifests)
        if self._store is None:
            on_progress("Linking imports across repositories")
            self._knowledge.save_graph(build_graph(outlines, commits, dependencies))
            self._knowledge.prune(names)
        else:
            for stale in previous.repositories:
                if stale.name not in names:
                    self._store.delete_repo(stale.name)

        state = WorkspaceState(
            repositories=tuple(repositories),
            graph=dependencies,
            indexed_at=datetime.now(UTC).isoformat(timespec="seconds"),
            knowledge_mode=self._settings.knowledge_mode,
        )
        self._workspace.replace(state)
        on_progress(f"Indexed {len(repositories)} repositories")
        return state

    def _write_knowledge(
        self, snapshot: RepoSnapshot, files: Sequence[SourceFile]
    ) -> list[FileOutline]:
        package_dirs = find_package_dirs(snapshot.root)
        outlines = [outline_file(file, package_dirs) for file in files]
        self._knowledge.save_repository(
            snapshot.name, build_pack(snapshot, files), build_map(snapshot, outlines)
        )
        return outlines

    def _chunk(self, files: Sequence[SourceFile]) -> list[Chunk]:
        return [
            chunk
            for file in files
            for chunk in chunk_file(
                file,
                max_lines=self._settings.chunk_max_lines,
                overlap_lines=self._settings.chunk_overlap_lines,
            )
        ]
