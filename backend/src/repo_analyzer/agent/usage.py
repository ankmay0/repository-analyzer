"""Token and latency accounting for one chat answer.

A single answer can take several model calls (the agent loops through tools), so
usage is summed over every model response in the LangGraph run. Counts come from
LangChain's ``usage_metadata`` on each ``AIMessage``; a model that does not
report usage simply contributes zeros.

``input_tokens`` follows LangChain's convention for Anthropic: it already
*includes* prompt-cache reads and writes. ``cache_read_tokens`` and
``cache_creation_tokens`` show how much of it came from, or went into, the cache.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage


@dataclass(slots=True)
class UsageTracker:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    _started: float = field(default_factory=time.perf_counter)
    _finished: float | None = None

    def add_model_response(self, message: AIMessage) -> None:
        """Count one model response and the tool calls it requested."""
        self.llm_calls += 1
        self.tool_calls += len(message.tool_calls)
        usage: Any = message.usage_metadata
        if not usage:
            return
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += int(usage.get("total_tokens") or input_tokens + output_tokens)
        details = usage.get("input_token_details") or {}
        self.cache_read_tokens += int(details.get("cache_read") or 0)
        self.cache_creation_tokens += int(details.get("cache_creation") or 0)

    def finish(self) -> None:
        if self._finished is None:
            self._finished = time.perf_counter()

    @property
    def latency_ms(self) -> int:
        end = self._finished if self._finished is not None else time.perf_counter()
        return round((end - self._started) * 1000)

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "latency_ms": self.latency_ms,
        }
