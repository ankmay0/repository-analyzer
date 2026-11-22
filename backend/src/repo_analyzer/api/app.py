"""FastAPI application factory.

Tests call ``create_app`` with their own settings and a scripted chat model.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel

from repo_analyzer import __version__
from repo_analyzer.api.benchmark_routes import router as benchmark_router
from repo_analyzer.api.routes import router
from repo_analyzer.api.services import build_services
from repo_analyzer.config import Settings
from repo_analyzer.errors import (
    AnalyzerError,
    BenchmarkRunInProgressError,
    BenchmarkUnavailableError,
    CtagsError,
    IndexingInProgressError,
    InvalidBenchmarkRequestError,
    InvalidRepositorySourceError,
    LLMNotConfiguredError,
    RepositoryFetchError,
    UnknownBenchmarkRunError,
    UnknownRepositoryError,
    WorkspaceEmptyError,
)
from repo_analyzer.retrieval.embeddings import Embedder

_STATUS_BY_ERROR: dict[type[AnalyzerError], int] = {
    InvalidRepositorySourceError: 400,
    UnknownRepositoryError: 404,
    IndexingInProgressError: 409,
    WorkspaceEmptyError: 409,
    RepositoryFetchError: 502,
    LLMNotConfiguredError: 503,
    CtagsError: 503,
    BenchmarkUnavailableError: 503,
    InvalidBenchmarkRequestError: 400,
    BenchmarkRunInProgressError: 409,
    UnknownBenchmarkRunError: 404,
}


def create_app(
    settings: Settings | None = None,
    *,
    model_factory: Callable[[], BaseChatModel] | None = None,
    benchmark_github_transport: httpx.BaseTransport | None = None,
    benchmark_embedder_factory: Callable[[], Embedder] | None = None,
) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Repository Analyzer", version=__version__)
    app.state.services = build_services(
        settings,
        model_factory=model_factory,
        benchmark_github_transport=benchmark_github_transport,
        benchmark_embedder_factory=benchmark_embedder_factory,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(AnalyzerError)
    async def handle_analyzer_error(_: Request, exc: AnalyzerError) -> JSONResponse:
        status = _STATUS_BY_ERROR.get(type(exc), 500)
        return JSONResponse(status_code=status, content={"detail": str(exc)})

    app.include_router(router, prefix="/api")
    app.include_router(benchmark_router, prefix="/api/benchmark")
    return app
