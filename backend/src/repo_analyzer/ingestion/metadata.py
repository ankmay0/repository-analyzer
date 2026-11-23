"""LLM-written endpoint metadata, generated once per repository at index time.

The Ctags JSON index says *what* is in a repository (files, symbols,
dependencies). This file adds *how data moves*: for each HTTP endpoint (and,
in front ends, each call to a backend API) the model records where the data
comes from, what kind of source that is, the tables involved, caching,
transformations, side effects and external calls.

It is written next to the index as ``<owner>__<name>.metadata.json``. Unlike the
index it is model output, so it can be wrong; every entry carries the
``file:line`` it was derived from so an answer built on it can be checked.

Generation is one model call per repository over the repository's source files
(truncated to a character budget). It is off by default
(``RA_ENDPOINT_METADATA``); the benchmark runner calls it directly.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from repo_analyzer.agent.usage import UsageTracker
from repo_analyzer.domain import SourceFile
from repo_analyzer.errors import AnalyzerError
from repo_analyzer.ingestion.repo_index import RepoIndex, index_filename

logger = logging.getLogger(__name__)

METADATA_SCHEMA_VERSION = 1
DEFAULT_MAX_SOURCE_CHARS = 150_000

SOURCE_TYPES = ("database", "cache", "external_api", "computed", "static", "file", "other")
ENTRY_KINDS = ("endpoint", "client_call", "job", "other")

# Languages worth sending to the model; docs and data files add cost, not facts.
_CODE_LANGUAGES = frozenset(
    {
        "python", "javascript", "typescript", "go", "rust", "java", "kotlin", "ruby",
        "php", "csharp", "c", "cpp", "sql", "graphql", "protobuf", "shell",
    }
)  # fmt: skip
_TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(_test|\.test|\.spec)\.")

_SYSTEM = """\
You document how data flows through a code base. You are given the source files of \
one repository with line numbers. Answer with one JSON object and nothing else.\
"""

_INSTRUCTIONS = """\
List every HTTP endpoint this repository serves (kind "endpoint") and, for front \
ends or clients, every call it makes to a backend API (kind "client_call"). Include \
scheduled or background jobs as kind "job". For each entry give:

- "kind": one of {kinds}
- "name": for endpoints "METHOD /path" exactly as routed (include router prefixes); \
for client calls the method and path called; for jobs the job name
- "handler": "path/to/file:function_name" that implements it
- "location": "path/to/file:line" where it is defined
- "data_source": one sentence on where the response data comes from
- "source_types": a list drawn from {source_types}
- "tables": database tables read or written (empty list if none)
- "caching": how the result is cached, with TTL and scope, or null if not cached
- "transformations": list of short sentences on how data is reshaped or computed \
(units, rounding, formulas, filters, sorting)
- "side_effects": list of writes, cache invalidations, or state changes
- "external_calls": list of external services or APIs called
- "notes": anything a reader would get wrong without reading the code (may be "")

Only state what the code shows. Return:
{{"entries": [ ... ]}}

Repository: {name}
"""


class MetadataGenerationError(AnalyzerError):
    """The model's endpoint metadata could not be produced or parsed."""


def metadata_filename(owner: str, name: str) -> str:
    return index_filename(owner, name).removesuffix(".json") + ".metadata.json"


def select_source_files(
    files: Iterable[SourceFile], *, max_chars: int = DEFAULT_MAX_SOURCE_CHARS
) -> tuple[str, list[str], bool]:
    """Render code files with line numbers; returns ``(text, paths, truncated)``."""
    parts: list[str] = []
    paths: list[str] = []
    used = 0
    truncated = False
    for file in sorted(files, key=lambda f: f.path):
        if file.language not in _CODE_LANGUAGES or _TEST_PATH.search(file.path):
            continue
        numbered = "\n".join(
            f"{n:>4}  {line}" for n, line in enumerate(file.text.splitlines(), start=1)
        )
        block = f"=== {file.path} ===\n{numbered}\n"
        if used + len(block) > max_chars:
            truncated = True
            continue
        parts.append(block)
        paths.append(file.path)
        used += len(block)
    return "".join(parts), paths, truncated


def parse_metadata_response(text: str) -> list[dict[str, Any]]:
    """Extract and normalise ``entries`` from the model's reply."""
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    else:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise MetadataGenerationError("The model did not return a JSON object")
        stripped = stripped[start : end + 1]
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise MetadataGenerationError(f"The model returned invalid JSON: {exc}") from exc
    raw_entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(raw_entries, list):
        raise MetadataGenerationError("The model's JSON has no 'entries' list")
    return [_normalize_entry(e) for e in raw_entries if isinstance(e, dict) and e.get("name")]


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _normalize_entry(entry: dict[str, Any]) -> dict[str, Any]:
    kind = _text(entry.get("kind"))
    caching = entry.get("caching")
    return {
        "kind": kind if kind in ENTRY_KINDS else "other",
        "name": _text(entry.get("name")),
        "handler": _text(entry.get("handler")),
        "location": _text(entry.get("location")),
        "data_source": _text(entry.get("data_source")),
        "source_types": [t for t in _strings(entry.get("source_types")) if t in SOURCE_TYPES],
        "tables": _strings(entry.get("tables")),
        "caching": _text(caching) or None,
        "transformations": _strings(entry.get("transformations")),
        "side_effects": _strings(entry.get("side_effects")),
        "external_calls": _strings(entry.get("external_calls")),
        "notes": _text(entry.get("notes")),
    }


def generate_endpoint_metadata(
    index: RepoIndex,
    files: Iterable[SourceFile],
    model: BaseChatModel,
    *,
    max_chars: int = DEFAULT_MAX_SOURCE_CHARS,
    usage: UsageTracker | None = None,
) -> dict[str, Any]:
    """Ask ``model`` once for the repository's endpoint metadata."""
    source_text, paths, truncated = select_source_files(files, max_chars=max_chars)
    if not paths:
        entries: list[dict[str, Any]] = []
    else:
        prompt = _INSTRUCTIONS.format(
            kinds=", ".join(ENTRY_KINDS),
            source_types=", ".join(SOURCE_TYPES),
            name=index["name"],
        )
        response = model.invoke([SystemMessage(_SYSTEM), HumanMessage(prompt + "\n" + source_text)])
        if not isinstance(response, AIMessage):
            raise MetadataGenerationError("The model returned no message")
        if usage is not None:
            usage.add_model_response(response)
        entries = parse_metadata_response(str(response.text))
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "repository": index["name"],
        "owner": index["owner"],
        "commit": index.get("commit"),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": getattr(model, "model", None) or getattr(model, "model_name", None),
        "files_read": paths,
        "truncated": truncated,
        "entries": entries,
    }


class MetadataStore:
    """Reads and writes ``<owner>__<name>.metadata.json`` next to the JSON indexes."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, owner: str, name: str) -> Path:
        return self._directory / metadata_filename(owner, name)

    def save(self, metadata: dict[str, Any]) -> Path:
        target = self.path_for(metadata["owner"], metadata["repository"])
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def load(self, owner: str, name: str) -> dict[str, Any] | None:
        path = self.path_for(owner, name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable metadata file %s: %s", path, exc)
            return None
        if not isinstance(data, dict) or data.get("schema_version") != METADATA_SCHEMA_VERSION:
            return None
        return data

    def delete(self, owner: str, name: str) -> None:
        self.path_for(owner, name).unlink(missing_ok=True)
