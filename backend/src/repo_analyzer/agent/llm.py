"""Construction of the chat model."""

from __future__ import annotations

from langchain_anthropic import ChatAnthropic
from langchain_core.language_models import BaseChatModel

from repo_analyzer.agent.service import AgentRole
from repo_analyzer.config import Settings
from repo_analyzer.errors import LLMNotConfiguredError


def build_chat_model(settings: Settings, role: AgentRole) -> BaseChatModel:
    """Build the Claude model for the hub or for the repository experts.

    Fails clearly when no credential is present. Experts use the hub's model and
    effort unless ``RA_EXPERT_MODEL`` / ``RA_EXPERT_EFFORT`` say otherwise.
    """
    model, effort = settings.llm_model, settings.llm_effort
    if role == "expert":
        model = settings.expert_model or model
        effort = settings.expert_effort or effort
    if settings.anthropic_api_key is None or not settings.anthropic_api_key.get_secret_value():
        raise LLMNotConfiguredError(
            "ANTHROPIC_API_KEY is not set on the server, so questions cannot be answered"
        )
    return ChatAnthropic(
        api_key=settings.anthropic_api_key,
        model=model,
        max_tokens=settings.llm_max_tokens,
        reasoning_effort=effort,
        timeout=settings.llm_timeout_seconds,
        max_retries=2,
    )
