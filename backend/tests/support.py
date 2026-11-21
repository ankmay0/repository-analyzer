"""Test helpers: fixture repository contents and a scripted chat model.

web-app --runtime--> mid-lib --runtime--> core-lib
web-app --dev------> core-lib            (also declared, but only for tests)
tools   --dev------> web-app             (reaches core-lib only through dev edges:
                                         tools -> web-app -> core-lib, two hops)
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

FIXTURE_REPOS: dict[str, dict[str, str]] = {
    "core-lib": {
        "pyproject.toml": '[project]\nname = "core-lib"\nversion = "1.0"\ndependencies = []\n',
        "src/core_lib/tokens.py": (
            "def sign_token(payload: str, secret: str) -> str:\n"
            '    """Sign a payload and return the token."""\n'
            '    return f"{payload}.{hash((payload, secret))}"\n'
            "\n\n"
            "class TokenError(Exception):\n"
            '    """Raised when a token cannot be verified."""\n\n'
            "    def describe(self) -> str:\n"
            '        return "bad token"\n'
        ),
        # Re-exports sign_token, so ``from core_lib import sign_token`` must be followed
        # through this file to the definition in tokens.py.
        "src/core_lib/__init__.py": "from .tokens import sign_token\n",
        ".env": "SECRET_KEY=do-not-index\n",
    },
    "mid-lib": {
        "pyproject.toml": (
            '[project]\nname = "mid_lib"\nversion = "1.0"\ndependencies = ["Core.Lib>=1.0"]\n'
        ),
        "src/mid_lib/__init__.py": "",  # an empty package marker: not indexed, still a package
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
        "scripts/smoke.py": (
            "from core_lib import sign_token\n\nprint(sign_token('smoke', 'web-app'))\n"
        ),
    },
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


Responder = Callable[[list[BaseMessage]], AIMessage]


class ScriptedChatModel(BaseChatModel):
    """A chat model that returns scripted responses and records what it was sent.

    Give it either ``responses`` (replayed in order) or a ``responder`` function
    that picks the reply from the messages it receives. The function form is for
    models shared by several agents running at once, where call order is not
    deterministic. It streams, like the real model, so the token-streaming path
    is exercised.
    """

    responses: list[AIMessage] = []  # noqa: RUF012 - pydantic copies field defaults
    responder: Responder | None = None
    calls: list[list[BaseMessage]] = []  # noqa: RUF012

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:  # type: ignore[override]
        return self

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        if self.responder is not None:
            return self.responder(messages)
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
            )
            yield ChatGenerationChunk(message=chunk)
            return
        text = str(message.content)
        for piece in (text[: len(text) // 2], text[len(text) // 2 :]):
            chunk = AIMessageChunk(content=piece)
            if run_manager:
                run_manager.on_llm_new_token(piece, chunk=ChatGenerationChunk(message=chunk))
            yield ChatGenerationChunk(message=chunk)


def tool_call(name: str, args: dict[str, Any], call_id: str = "call-1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def consult(repo: str, question: str, call_id: str | None = None) -> AIMessage:
    """A hub message that delegates ``question`` to the expert on ``repo``."""
    return tool_call(
        "consult_repository", {"repo": repo, "question": question}, call_id or f"ask-{repo}"
    )


def expert_repo(messages: list[BaseMessage]) -> str:
    """Which repository's expert is being called, read from its system prompt."""
    return str(messages[0].content).split("expert on the `", 1)[1].split("`", 1)[0]


def searching_expert(text: str = "sign_token") -> Responder:
    """An expert that runs one exact-text search, then reports what it found."""

    def respond(messages: list[BaseMessage]) -> AIMessage:
        repo = expert_repo(messages)
        if isinstance(messages[-1], ToolMessage):
            found = "No lines contain" not in str(messages[-1].content)
            return AIMessage(content=f"{repo} {'uses' if found else 'does not use'} {text}.")
        return tool_call("find_references", {"text": text}, call_id=f"{repo}-search")

    return respond
