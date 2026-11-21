"""FastAPI application factory.

Tests call ``create_app`` with their own settings and a scripted chat model.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from repo_analyzer import __version__
from repo_analyzer.agent.service import ModelFactory
from repo_analyzer.api.routes import router
from repo_analyzer.api.services import build_services
from repo_analyzer.config import Settings
from repo_analyzer.errors import (
    AnalyzerError,
    IndexingInProgressError,
    InvalidRepositorySourceError,
    LLMNotConfiguredError,
    ReindexRequiredError,
    RepositoryFetchError,
    UnknownRepositoryError,
    WorkspaceEmptyError,
)

_STATUS_BY_ERROR: dict[type[AnalyzerError], int] = {
    InvalidRepositorySourceError: 400,
    UnknownRepositoryError: 404,
    IndexingInProgressError: 409,
    WorkspaceEmptyError: 409,
    ReindexRequiredError: 409,
    RepositoryFetchError: 502,
    LLMNotConfiguredError: 503,
}


def create_app(
    settings: Settings | None = None,
    *,
    model_factory: ModelFactory | None = None,
) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(title="Repository Analyzer", version=__version__)
    app.state.services = build_services(settings, model_factory=model_factory)

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
    return app
