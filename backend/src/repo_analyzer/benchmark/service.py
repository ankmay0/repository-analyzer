"""Shared setup for the compare API and the CLI benchmark: credentials, fixtures, tools.

This is not a web runner. Known-answer runs go through ``python -m repo_analyzer.benchmark``.
The compare endpoint uses this to decide which approaches can run and to build embedders
and the fixture workspace.
"""

from __future__ import annotations

from collections.abc import Callable

from langchain_core.language_models import BaseChatModel

from repo_analyzer.benchmark.questions import QuestionSet, load_questions
from repo_analyzer.config import Settings
from repo_analyzer.errors import BenchmarkUnavailableError, InvalidBenchmarkRequestError
from repo_analyzer.retrieval.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    Embedder,
    FastEmbedEmbedder,
    fastembed_available,
)

NO_FASTEMBED = 'fastembed is not installed (pip install -e ".[embeddings]")'


class BenchmarkService:
    def __init__(
        self,
        settings: Settings,
        model_factory: Callable[[], BaseChatModel],
        *,
        needs_api_key: bool = True,
        embedder_factory: Callable[[], Embedder] | None = None,
    ) -> None:
        self._settings = settings
        self._model_factory = model_factory
        self._needs_api_key = needs_api_key
        self._embedder_factory = embedder_factory

    @property
    def llm_configured(self) -> bool:
        key = self._settings.anthropic_api_key
        return not self._needs_api_key or bool(key and key.get_secret_value())

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def prompt_caching(self) -> bool:
        """Scripted test models reject provider-specific cache options."""
        return self._needs_api_key

    def make_model(self) -> BaseChatModel:
        return self._model_factory()

    def embedder_factory(self) -> Callable[[], Embedder] | None:
        """How to build an embedder, or None when the embeddings extra is missing."""
        return self._make_embedder if self.unavailable_reason("json-embeddings") is None else None

    def fixture_repositories(self) -> list[str]:
        """Fixture apps that exist on disk (from the questions file's repository list)."""
        return self._select_repositories(self.question_set(), None)

    def _make_embedder(self) -> Embedder:
        if self._embedder_factory is not None:
            return self._embedder_factory()
        return FastEmbedEmbedder(DEFAULT_EMBEDDING_MODEL)

    def unavailable_reason(self, approach: str) -> str | None:
        if (
            approach == "json-embeddings"
            and self._embedder_factory is None
            and not fastembed_available()
        ):
            return NO_FASTEMBED
        return None

    def question_set(self) -> QuestionSet:
        path = self._settings.benchmarks_dir / "questions.yaml"
        if not path.is_file():
            raise BenchmarkUnavailableError(
                f"Benchmark questions not found at {path}. Run the server from a source checkout "
                "or set RA_BENCHMARKS_DIR."
            )
        return load_questions(path)

    def _select_repositories(self, question_set: QuestionSet, names: list[str] | None) -> list[str]:
        fixtures = self._settings.benchmarks_dir / "fixtures"
        available = [r for r in question_set.repositories if (fixtures / r).is_dir()]
        if not names:
            if not available:
                raise BenchmarkUnavailableError(f"No fixture apps found in {fixtures}")
            return available
        if unknown := [n for n in names if n not in available]:
            raise InvalidBenchmarkRequestError(
                f"Unknown fixture repository: {', '.join(unknown)} (available: "
                f"{', '.join(available) or 'none'})"
            )
        return [r for r in available if r in set(names)]
