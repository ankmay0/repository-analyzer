"""Test helpers: fixture repository contents and a scripted chat model.

Each fixture repository is a tiny Python project; ``mid-lib`` declares a dependency on
``core-lib``, ``web-app`` on ``mid-lib`` (and on ``core-lib`` for development only).
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.messages.ai import UsageMetadata
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from repo_analyzer.ingestion.graph_extract import GraphRecords
from repo_analyzer.ingestion.graph_store import (
    MAX_TRACE_DEPTH,
    Trace,
    assemble_trace,
    match_endpoint,
)

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


class FunctionChatModel(ScriptedChatModel):
    """A chat model whose reply is computed from the messages it receives."""

    responses: list[AIMessage] = []  # noqa: RUF012 - unused; ``respond`` decides
    respond: Callable[[list[BaseMessage]], AIMessage]

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        self.calls.append(list(messages))
        return self.respond(messages)


class FakeGraphStore:
    """In-memory ``GraphStore`` for tests: same API as the Neo4j store, no database.

    Traces are built from the stored records with the same ``assemble_trace`` the Neo4j
    store uses, so tests exercise the real ordering and formatting rules.
    """

    def __init__(self, *, fail: bool = False, reachable: bool = True) -> None:
        self.repos: dict[tuple[str, str], GraphRecords] = {}
        self.commits: dict[tuple[str, str], str | None] = {}
        self.deleted: list[tuple[str, str]] = []
        self.fail = fail
        self.reachable = reachable
        self.schema_calls = 0
        self.closed = False

    def ensure_schema(self) -> None:
        self.schema_calls += 1

    def ping(self) -> bool:
        return self.reachable

    def sync_repo(self, ws: str, repo: str, commit: str | None, records: GraphRecords) -> None:
        if self.fail:
            raise ConnectionError("simulated Neo4j outage")
        self.repos[(ws, repo)] = records
        self.commits[(ws, repo)] = commit

    def delete_repo(self, ws: str, repo: str) -> None:
        if self.fail:
            raise ConnectionError("simulated Neo4j outage")
        self.repos.pop((ws, repo), None)
        self.deleted.append((ws, repo))

    def endpoints(self, ws: str, repo: str) -> list[dict[str, Any]]:
        records = self.repos.get((ws, repo), GraphRecords())
        return [asdict(e) for e in records.endpoints]

    def trace_endpoint(self, ws: str, repo: str, method: str, path: str) -> Trace | None:
        endpoint = match_endpoint(self.endpoints(ws, repo), method, path)
        if endpoint is None:
            return None
        records = self.repos[(ws, repo)]
        nodes = {
            f.id: {
                "id": f.id, "name": f.name, "path": f.path, "line": f.line,
                "end_line": f.end_line, "unresolved": list(f.unresolved),
            }
            for f in records.functions
        }  # fmt: skip
        outgoing: dict[str, list[Any]] = defaultdict(list)
        for call in records.calls:
            outgoing[call.caller].append(call)
        paths: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

        def walk(path_nodes: list[dict[str, Any]], rels: list[dict[str, Any]]) -> None:
            paths.append((path_nodes, rels))
            if len(rels) == MAX_TRACE_DEPTH:
                return
            for call in outgoing[path_nodes[-1]["id"]]:
                if all(node["id"] != call.callee for node in path_nodes):
                    walk(
                        [*path_nodes, nodes[call.callee]],
                        [*rels, {"via": call.via, "line": call.line}],
                    )

        walk([nodes[endpoint["handler"]]], [])
        return assemble_trace(endpoint, paths, [asdict(a) for a in records.accesses])

    def neighbours(self, ws: str, repo: str, function: str, direction: str) -> list[dict[str, Any]]:
        records = self.repos.get((ws, repo), GraphRecords())
        by_id = {f.id: f for f in records.functions}

        def matches(fid: str) -> bool:
            name = by_id[fid].name
            return function in (name, fid) or name.endswith("." + function)

        rows = []
        for call in records.calls:
            target, other = (
                (call.callee, call.caller) if direction == "in" else (call.caller, call.callee)
            )
            if matches(target):
                f = by_id[other]
                rows.append({
                    "id": f.id, "name": f.name, "path": f.path, "line": f.line,
                    "end_line": f.end_line, "via": call.via, "call_line": call.line,
                    "target": target,
                })  # fmt: skip
        return rows

    def close(self) -> None:
        self.closed = True
