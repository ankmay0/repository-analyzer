"""One-off preparation shared by the benchmark runner and the compare endpoint.

Indexing the fixture apps, writing endpoint metadata, embedding code chunks and loading
the code graph are the steps some approaches need before they can answer. They live here
so the two callers do the same thing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.usage import UsageTracker
from repo_analyzer.benchmark.approaches import ApproachUnavailableError
from repo_analyzer.config import Settings
from repo_analyzer.domain import SourceFile
from repo_analyzer.ingestion.fetcher import RepoFetcher
from repo_analyzer.ingestion.graph_extract import extract_graph
from repo_analyzer.ingestion.graph_store import GraphStore
from repo_analyzer.ingestion.metadata import MetadataStore, generate_endpoint_metadata
from repo_analyzer.ingestion.pipeline import IndexingService
from repo_analyzer.ingestion.repo_index import RepoIndexStore
from repo_analyzer.ingestion.scanner import scan_repository
from repo_analyzer.retrieval.embeddings import Embedder, EmbeddingIndex, chunk_files
from repo_analyzer.workspace import IndexedRepository, Workspace

GRAPH_DISABLED = "the code graph is off (set RA_GRAPH_ENABLED=true and NEO4J_PASSWORD)"
GRAPH_UNREACHABLE = "Neo4j is not reachable (see the server log)"


def graph_unavailable_reason(store: GraphStore | None) -> str | None:
    if store is None:
        return GRAPH_DISABLED
    return None if store.ping() else GRAPH_UNREACHABLE


@dataclass(slots=True)
class SetupCost:
    """One-off work an approach needs before questions (metadata, embeddings)."""

    approach: str
    description: str
    usage: dict[str, int]


def index_fixture_repositories(
    settings: Settings,
    index_store: RepoIndexStore,
    workspace: Workspace,
    fixtures_dir: Path,
    names: Sequence[str],
) -> tuple[IndexedRepository, ...]:
    """Index fixture folders with Ctags. ``settings`` must have ``allow_local_paths`` on."""
    fetcher = RepoFetcher(
        settings.checkouts_dir,
        allowed_hosts=settings.allowed_git_hosts,
        allow_local_paths=True,
    )
    service = IndexingService(settings, fetcher, index_store, workspace)
    return service.run([str(fixtures_dir / name) for name in names]).repositories


class ApproachSetup:
    """Builds what ``json-metadata``, ``json-embeddings`` and ``json-graph`` need."""

    def __init__(
        self,
        settings: Settings,
        index_store: RepoIndexStore,
        workspace: Workspace,
        model_factory: Callable[[], BaseChatModel],
        embedder_factory: Callable[[], Embedder] | None = None,
    ) -> None:
        self._settings = settings
        self._index_store = index_store
        self._workspace = workspace
        self._model_factory = model_factory
        self._embedder_factory = embedder_factory

    def source_files(self, name: str) -> list[SourceFile]:
        repository = self._workspace.state.repository(name)
        return list(
            scan_repository(
                name, Path(repository.root), max_file_bytes=self._settings.max_file_bytes
            )
        )

    def generate_metadata(self, *, reuse: bool = False) -> tuple[MetadataStore, SetupCost]:
        """Ask the model once per repository for endpoint metadata and save it.

        With ``reuse``, a repository that already has a metadata file keeps it.
        """
        store = MetadataStore(self._settings.repos_dir)
        usage = UsageTracker()
        model = self._model_factory()
        generated = 0
        for repository in self._workspace.state.repositories:
            if reuse and store.load(repository.owner, repository.name) is not None:
                continue
            index = self._index_store.load(repository.index_file)
            metadata = generate_endpoint_metadata(
                index, self.source_files(repository.name), model, usage=usage
            )
            store.save(metadata)
            generated += 1
        usage.finish()
        description = "endpoint metadata, one model call per repository"
        if reuse and generated == 0:
            description = "endpoint metadata already on disk (no model call)"
        return store, SetupCost("json-metadata", description, usage.to_dict())

    def build_embeddings(self, save_to: Path | None = None) -> tuple[EmbeddingIndex, SetupCost]:
        if self._embedder_factory is None:
            raise ApproachUnavailableError("no embedder configured")
        usage = UsageTracker()
        embedder = self._embedder_factory()
        chunks = [
            chunk
            for repository in self._workspace.state.repositories
            for chunk in chunk_files(self.source_files(repository.name))
        ]
        index = EmbeddingIndex.build(embedder, chunks)
        if save_to is not None:
            index.save(save_to)
        usage.finish()
        cost = SetupCost(
            "json-embeddings",
            f"embedded {len(chunks)} chunks with {embedder.name} (local, no LLM tokens)",
            usage.to_dict(),
        )
        return index, cost

    def build_graph(self, store: GraphStore | None, ws: str) -> SetupCost:
        """Load every workspace repository into the code graph under ``ws``."""
        reason = graph_unavailable_reason(store)
        if store is None or reason is not None:
            raise ApproachUnavailableError(reason or GRAPH_DISABLED)
        usage = UsageTracker()
        repositories = self._workspace.state.repositories
        for repository in repositories:
            records = extract_graph(self.source_files(repository.name))
            store.sync_repo(ws, repository.name, repository.commit, records)
        usage.finish()
        return SetupCost(
            "json-graph",
            f"code graph for {len(repositories)} repositories (local parsing, no LLM tokens)",
            usage.to_dict(),
        )
