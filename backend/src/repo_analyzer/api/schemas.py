"""Request and response models for the HTTP API."""

from __future__ import annotations

from pydantic import BaseModel, Field

from repo_analyzer.ingestion.jobs import JobStatus
from repo_analyzer.workspace import WorkspaceState


class IndexRequest(BaseModel):
    repositories: list[str] = Field(min_length=1, description="Repository URLs to index.")


class ChatRequest(BaseModel):
    thread_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    message: str = Field(min_length=1, max_length=8_000)


class RepositoryOut(BaseModel):
    name: str
    owner: str
    source: str
    commit: str | None
    default_branch: str | None
    file_count: int
    symbol_count: int
    dependency_count: int
    index_file: str


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
    indexed_at: str | None
    job: JobOut

    @classmethod
    def build(cls, state: WorkspaceState, status: JobStatus) -> WorkspaceOut:
        return cls(
            repositories=[
                RepositoryOut(
                    name=r.name,
                    owner=r.owner,
                    source=r.source,
                    commit=r.commit,
                    default_branch=r.default_branch,
                    file_count=r.file_count,
                    symbol_count=r.symbol_count,
                    dependency_count=r.dependency_count,
                    index_file=r.index_file,
                )
                for r in state.repositories
            ],
            indexed_at=state.indexed_at,
            job=JobOut.from_status(status),
        )


class HealthOut(BaseModel):
    status: str
    llm_configured: bool
    llm_model: str
    ctags_available: bool
    ctags_version: str | None
    graph_available: bool  # RA_GRAPH_ENABLED and Neo4j reachable


class SkillOut(BaseModel):
    name: str
    description: str
