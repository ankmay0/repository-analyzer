"""Test helpers: fixture repository contents and a scripted chat model.

Each fixture repository is a tiny Python project; ``mid-lib`` declares a dependency on
``core-lib``, ``web-app`` on ``mid-lib`` (and on ``core-lib`` for development only).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

FIXTURE_REPOS: dict[str, dict[str, str]] = {
    "core-lib": {
        "pyproject.toml": '[project]\nname = "core-lib"\nversion = "1.0"\ndependencies = []\n',
        "src/core_lib/tokens.py": (
            "def sign_token(payload: str, secret: str) -> str:\n"
            '    """Sign a payload and return the token."""\n'
            '    return f"{payload}.{hash((payload, secret))}"\n'
        ),
        ".env": "SECRET_KEY=do-not-index\n",
    },
    "mid-lib": {
        "pyproject.toml": (
            '[project]\nname = "mid_lib"\nversion = "1.0"\ndependencies = ["Core.Lib>=1.0"]\n'
        ),
        "src/mid_lib/session.py": (
            "from core_lib.tokens import sign_token\n\n\n"
            "def create_session(user: str) -> str:\n"
            '    """Create a login session for a user."""\n'
            '    return sign_token(user, "secret")\n'
        ),
    },
    "web-app": {
        "pyproject.toml": (
            '[project]\nname = "web-app"\nversion = "1.0"\ndependencies = ["mid-lib"]\n\n'
            '[dependency-groups]\ndev = ["core-lib"]\n'
        ),
        "app/login.py": (
            "from mid_lib.session import create_session\n\n\n"
            "def login_view(user: str) -> dict:\n"
            '    return {"session": create_session(user)}\n'
        ),
        "README.md": "# web-app\n\nThe customer-facing web application.\n",
    },
    "tools": {
        "package.json": json.dumps({"name": "tools", "devDependencies": {"left-pad": "1.0.0"}}),
        "requirements-dev.txt": "web-app==1.0\n",
        "scripts/smoke.py": "print('smoke test against web-app')\n",
    },
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


class ScriptedChatModel(BaseChatModel):
    """A chat model that replays a fixed list of responses and records its inputs.

    It streams, like the real model, so the token-streaming path is exercised.
    """

    responses: list[AIMessage]
    calls: list[list[BaseMessage]] = []  # noqa: RUF012 - pydantic copies field defaults

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if len(self.calls) > len(self.responses):
            raise AssertionError("ScriptedChatModel ran out of scripted responses")
        return self.responses[len(self.calls) - 1]

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._next(messages)
        if message.tool_calls:
            chunk = AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": call["name"],
                        "args": json.dumps(call["args"]),
                        "id": call["id"],
                        "index": index,
                    }
                    for index, call in enumerate(message.tool_calls)
                ],
                usage_metadata=message.usage_metadata,
            )
            yield ChatGenerationChunk(message=chunk)
            return
        text = str(message.content)
        pieces = (text[: len(text) // 2], text[len(text) // 2 :])
        for position, piece in enumerate(pieces):
            # Like Anthropic, report usage once, on the final chunk.
            last = position == len(pieces) - 1
            chunk = AIMessageChunk(
                content=piece, usage_metadata=message.usage_metadata if last else None
            )
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


def tool_call(
    name: str,
    args: dict[str, Any],
    call_id: str = "call-1",
    usage: UsageMetadata | None = None,
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": call_id}],
        usage_metadata=usage,
    )


def usage(
    input_tokens: int, output_tokens: int, *, cache_read: int = 0, cache_creation: int = 0
) -> UsageMetadata:
    """Usage in LangChain's shape; ``input_tokens`` includes the cache tokens."""
    return UsageMetadata(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        input_token_details={"cache_read": cache_read, "cache_creation": cache_creation},
    )
