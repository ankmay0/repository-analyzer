"""Request and response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from repo_analyzer.domain import DependencyEdge
from repo_analyzer.ingestion.dependency_graph import ImpactedRepository
from repo_analyzer.ingestion.jobs import JobStatus
from repo_analyzer.workspace import WorkspaceState


class IndexRequest(BaseModel):
    repositories: list[str] = Field(min_length=1, description="Repository URLs to index.")


class ChatRequest(BaseModel):
    thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(min_length=1, max_length=8_000)


class RepositoryOut(BaseModel):
    name: str
    source: str
    commit: str | None
    file_count: int
    chunk_count: int
    symbol_count: int


class EdgeOut(BaseModel):
    source_repo: str
    target_repo: str
    ecosystem: str
    package: str
    scope: str
    manifest_path: str

    @classmethod
    def from_edge(cls, edge: DependencyEdge) -> EdgeOut:
        return cls(
            source_repo=edge.source_repo,
            target_repo=edge.target_repo,
            ecosystem=edge.package.ecosystem.value,
            package=edge.package.name,
            scope=edge.scope.value,
            manifest_path=edge.manifest_path,
        )


class JobOut(BaseModel):
    state: str
    message: str
    error: str | None
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_status(cls, status: JobStatus) -> JobOut:
        return cls(
            state=status.state.value,
            message=status.message,
            error=status.error,
            started_at=status.started_at,
            finished_at=status.finished_at,
        )


class WorkspaceOut(BaseModel):
    repositories: list[RepositoryOut]
    edges: list[EdgeOut]
    indexed_at: str | None
    knowledge_mode: str
    job: JobOut

    @classmethod
    def build(cls, state: WorkspaceState, status: JobStatus) -> WorkspaceOut:
        return cls(
            repositories=[
                RepositoryOut(
                    name=r.name,
                    source=r.source,
                    commit=r.commit,
                    file_count=r.file_count,
                    chunk_count=r.chunk_count,
                    symbol_count=r.symbol_count,
                )
                for r in state.repositories
            ],
            edges=[EdgeOut.from_edge(e) for e in state.graph.edges],
            indexed_at=state.indexed_at,
            knowledge_mode=state.knowledge_mode,
            job=JobOut.from_status(status),
        )


class ImpactedOut(BaseModel):
    repo: str
    distance: int
    weak: bool
    path: list[EdgeOut]

    @classmethod
    def from_impacted(cls, item: ImpactedRepository) -> ImpactedOut:
        return cls(
            repo=item.repo,
            distance=item.distance,
            weak=item.weak,
            path=[EdgeOut.from_edge(e) for e in item.path],
        )


class ImpactOut(BaseModel):
    repo: str
    impacted: list[ImpactedOut]


class HealthOut(BaseModel):
    status: str
    llm_configured: bool
    llm_model: str
    knowledge_mode: str
    embedder: str | None
