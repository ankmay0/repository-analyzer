from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest
from langchain_anthropic import ChatAnthropic

from repo_analyzer.agent.llm import build_chat_model
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError
from repo_analyzer.retrieval.embeddings import FastEmbedEmbedder, HashingEmbedder, build_embedder


def test_chat_model_requires_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMNotConfiguredError, match="ANTHROPIC_API_KEY"):
        build_chat_model(Settings(_env_file=None))


def test_chat_model_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    model = build_chat_model(Settings(_env_file=None, llm_effort="high", llm_max_tokens=2048))

    assert isinstance(model, ChatAnthropic)
    assert model.model == "claude-opus-5-5"
    assert model.max_tokens == 2048
    assert model.reasoning_effort == "high"


def test_api_key_is_read_from_a_dotenv_file_and_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=from-dotenv\nRA_LLM_EFFORT=low\n")

    settings = Settings(_env_file=env_file)

    assert settings.llm_effort == "low"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "from-dotenv"
    assert "from-dotenv" not in repr(settings)
    assert isinstance(build_chat_model(settings), ChatAnthropic)


def test_embedder_is_selected_by_settings() -> None:
    assert isinstance(
        build_embedder(Settings(_env_file=None, embedding_provider="hashing")), HashingEmbedder
    )
    default = build_embedder(Settings(_env_file=None))
    assert isinstance(default, FastEmbedEmbedder)
    assert default.name == "fastembed:BAAI/bge-small-en-v1.5"


def test_fastembed_adapter_loads_lazily_and_converts_vectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercises the adapter against a stand-in for the fastembed model class."""
    loaded: list[str] = []

    class StubTextEmbedding:
        def __init__(self, model_name: str) -> None:
            loaded.append(model_name)

        def embed(self, texts: list[str]):  # type: ignore[no-untyped-def]
            return (np.full(3, float(len(text))) for text in texts)

        def query_embed(self, text: str):  # type: ignore[no-untyped-def]
            yield np.full(3, -1.0)

    monkeypatch.setitem(
        sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=StubTextEmbedding)
    )

    embedder = FastEmbedEmbedder("some/model")
    assert loaded == []  # nothing is loaded until the first embedding is requested

    assert embedder.embed_documents(["a", "bbb"]) == [[1.0, 1.0, 1.0], [3.0, 3.0, 3.0]]
    assert embedder.embed_query("q") == [-1.0, -1.0, -1.0]
    assert loaded == ["some/model"]  # and only once
