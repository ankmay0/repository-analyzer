"""Construction of the chat model."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError


def build_chat_model(settings: Settings) -> BaseChatModel:
    """Build the Claude chat model, failing clearly when no credential is present."""
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise LLMNotConfiguredError(
            "ANTHROPIC_API_KEY is not set on the server, so questions cannot be answered"
        )
    return ChatAnthropic(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        max_tokens=settings.llm_max_tokens,
        reasoning_effort=settings.llm_effort,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )
