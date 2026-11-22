"""Extract functions, classes and imports by running Universal Ctags once per repository.

The JSON index lists only top-level functions, classes (and methods named
``Class.method``), and the imports that Ctags itself reports. Everything else
Ctags emits — variables, constants, properties, type aliases, enumerators, and
symbols nested inside a function — is dropped.

Imports come from Ctags reference tags (``--extras=+r``). Coverage varies by
language: Python and a few others are reasonably complete; JavaScript and
TypeScript currently report almost none. Where Ctags gives nothing, the import
list is empty.

This module talks to Ctags through ``subprocess`` with an argument list (no
shell). It does not follow symlinks. If the binary is missing, is not Universal
Ctags, or the run fails, indexing fails with ``CtagsError``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from repo_analyzer.errors import CtagsError

logger = logging.getLogger(__name__)

CTAGS_TIMEOUT_SECONDS = 120.0
CTAGS_PROBE_TIMEOUT_SECONDS = 5.0

# --- Public value types (unchanged JSON shape) --------------------------------


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    line: int  # 1-based
    end_line: int | None = None  # only when Ctags provides ``end``

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "line": self.line}
        if self.end_line is not None and self.end_line != self.line:
            data["end_line"] = self.end_line
        return data


@dataclass(frozen=True, slots=True)
class ImportRef:
    module: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {"module": self.module, "line": self.line}


@dataclass(frozen=True, slots=True)
class FileSymbols:
    functions: tuple[Symbol, ...] = ()
    classes: tuple[Symbol, ...] = ()
    imports: tuple[ImportRef, ...] = ()

    def is_empty(self) -> bool:
        return not (self.functions or self.classes or self.imports)

    def to_dict(self) -> dict[str, Any]:
        return {
            "functions": [f.to_dict() for f in self.functions],
            "classes": [c.to_dict() for c in self.classes],
            "imports": [i.to_dict() for i in self.imports],
        }


# --- Kind mapping -------------------------------------------------------------
#
# Functions: function, func, generator, and (when not nested) method-like kinds.
# Methods are stored in the ``functions`` list as ``Class.method``.
#   - ``member`` is a Python method, but a field in C, C++ and Go, so it is
#     kept only when the language is Python.
#   - A ``func``/``function`` whose scope is a struct, class, trait, etc. is a
#     method (Go, PHP). A ``method`` with no scope is a top-level function
#     (Kotlin).
#
# Classes: class, struct, interface, trait, union, object (Kotlin), and Ruby
# modules. Interfaces are included because they are the type you look up and
# attach methods to; type aliases, enums, typedefs, packages and Rust
# ``implementation`` blocks are left out.
# Protobuf ``message`` and ``service`` are treated as classes; ``rpc`` as a
# method of the service.
#
# Dropped: variable, constant, property, field, enumerator, alias, enum,
# namespace, package (as a definition), parameter, local, and anything whose
# scope is a function (helpers and locals nested inside one).

Category = Literal["function", "class", "import"]

_FUNCTION_KINDS = frozenset({"function", "func", "generator", "rpc"})
_METHOD_KINDS = frozenset({"method", "getter", "setter", "singletonMethod", "methodSpec"})
_CLASS_KINDS = frozenset(
    {
        "class",
        "struct",
        "interface",
        "trait",
        "union",
        "message",  # protobuf
        "service",  # protobuf
    }
)
# Kotlin ``object`` declarations (JSON also uses kind "object" for maps, so this
# is language-restricted below).
_KOTLIN_OBJECT_KIND = "object"

# Markup and data formats emit "class" tags for CSS selectors, HTML class
# attributes, JSON objects, and so on. Those are not code symbols.
_NON_CODE_LANGUAGES = frozenset(
    {
        "HTML",
        "CSS",
        "JSON",
        "Markdown",
        "YAML",
        "XML",
        "SVG",
        "DTD",
        "TOML",
        "Ini",
        "Iniconf",
        "Maven2",
        "ReStructuredText",
        "AsciiDoc",
        "Text",
    }
)
# Ruby modules are class-like; Python/Go "module"/"package" tags are imports or
# the file's own package declaration, never a class.
_RUBY_MODULE_KIND = "module"

_NESTED_SCOPE_KINDS = frozenset(
    {
        "function",
        "func",
        "method",
        "member",
        "generator",
        "singletonMethod",
        "getter",
        "setter",
        "rpc",
    }
)
_PACKAGE_SCOPE_KINDS = frozenset({"package", "namespace"})
_TYPE_SCOPE_KINDS = frozenset(
    {
        "class",
        "struct",
        "interface",
        "trait",
        "union",
        "object",
        "module",
        "implementation",  # Rust impl: qualify methods as Type.method, but not a class
        "message",
        "service",
    }
)
_IMPORT_ROLES = frozenset(
    {
        "imported",
        "indirectlyImported",
        "namespace",
        "required",
        "loaded",
        "system",
        "local",
        "included",
    }
)
_IMPORT_KINDS = frozenset({"module", "package", "library", "header", "script", "unknown"})


def _roles(tag: dict[str, Any]) -> set[str]:
    raw = tag.get("roles")
    if not raw or not isinstance(raw, str):
        return set()
    return {part for part in raw.replace(",", " ").split() if part}


def _kind(tag: dict[str, Any]) -> str:
    value = tag.get("kind")
    return value if isinstance(value, str) else ""


def _language(tag: dict[str, Any]) -> str:
    value = tag.get("language")
    return value if isinstance(value, str) else ""


def _scope(tag: dict[str, Any]) -> tuple[str, str]:
    raw_scope = tag.get("scope")
    raw_kind = tag.get("scopeKind")
    scope = raw_scope if isinstance(raw_scope, str) else ""
    scope_kind = raw_kind if isinstance(raw_kind, str) else ""
    return scope, scope_kind


def _line(tag: dict[str, Any]) -> int | None:
    value = tag.get("line")
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) >= 1:
        return int(value)
    return None


def _end_line(tag: dict[str, Any], start: int) -> int | None:
    value = tag.get("end")
    if isinstance(value, int) and value >= start:
        return value
    if isinstance(value, str) and value.isdigit() and int(value) >= start:
        return int(value)
    return None


def _is_nested_in_function(scope_kind: str) -> bool:
    return scope_kind in _NESTED_SCOPE_KINDS


def _qualified_name(name: str, scope: str, scope_kind: str) -> str:
    """Build ``Class.method`` / ``Service.Config``. Go's ``package.Type`` becomes ``Type``."""
    if not scope or scope_kind in _PACKAGE_SCOPE_KINDS:
        return name
    if scope_kind in {"struct", "interface"} and "." in scope:
        scope = scope.rsplit(".", 1)[-1]
    return f"{scope}.{name}"


def classify_ctags_tag(tag: dict[str, Any]) -> tuple[Category, Symbol | ImportRef] | None:
    """Map one Ctags JSON object to a function, class or import, or ``None`` to drop it."""
    name = tag.get("name")
    if not isinstance(name, str) or not name:
        return None
    start = _line(tag)
    if start is None:
        return None
    kind = _kind(tag)
    language = _language(tag)
    scope, scope_kind = _scope(tag)
    roles = _roles(tag)

    if language in _NON_CODE_LANGUAGES:
        return None

    if roles & _IMPORT_ROLES and kind in _IMPORT_KINDS:
        # Prefer the module being imported: for ``from pkg import Name`` Ctags
        # emits both the module (kind=module, roles=namespace) and the name
        # (kind=unknown, scope=pkg). Both become ``pkg`` and are de-duplicated.
        module = scope if kind == "unknown" and scope else name
        if not module:
            return None
        return "import", ImportRef(module, start)

    if _is_nested_in_function(scope_kind):
        return None

    is_kotlin_object = kind == _KOTLIN_OBJECT_KIND and language == "Kotlin"
    if (
        kind in _CLASS_KINDS
        or (kind == _RUBY_MODULE_KIND and language == "Ruby")
        or is_kotlin_object
    ):
        qualified = _qualified_name(name, scope, scope_kind)
        return "class", Symbol(qualified, start, _end_line(tag, start))

    is_python_method = kind == "member" and language == "Python" and scope_kind in _TYPE_SCOPE_KINDS
    is_method = kind in _METHOD_KINDS or is_python_method
    is_function = kind in _FUNCTION_KINDS
    if not (is_method or is_function):
        return None

    if (is_method or scope_kind in _TYPE_SCOPE_KINDS) and scope and scope_kind in _TYPE_SCOPE_KINDS:
        display = _qualified_name(name, scope, scope_kind)
    else:
        display = name
    return "function", Symbol(display, start, _end_line(tag, start))


def parse_ctags_json_lines(
    lines: Iterable[str], *, root: Path | None = None
) -> dict[str, FileSymbols]:
    """Parse Ctags ``--output-format=json`` lines into per-file symbol lists.

    ``root``, when given, is used to make absolute paths relative to the checkout.
    Malformed lines and pseudo-tags are ignored. This function is pure given its
    inputs, so tests can feed it canned output without running Ctags.
    """
    collected: dict[str, _Bucket] = defaultdict(_Bucket)
    for raw in lines:
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            tag = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(tag, dict) or tag.get("_type") not in (None, "tag"):
            continue
        path = _tag_path(tag, root)
        if path is None:
            continue
        mapped = classify_ctags_tag(tag)
        if mapped is None:
            continue
        category, item = mapped
        collected[path].add(category, item)
    return {path: bucket.to_symbols() for path, bucket in collected.items()}


class _Bucket:
    def __init__(self) -> None:
        self.functions: list[Symbol] = []
        self.classes: list[Symbol] = []
        self.imports: list[ImportRef] = []
        self._seen: set[tuple[str, str, int]] = set()

    def add(self, category: Category, item: Symbol | ImportRef) -> None:
        key = (
            category,
            item.name if isinstance(item, Symbol) else item.module,
            item.line,
        )
        if key in self._seen:
            return
        self._seen.add(key)
        if category == "function" and isinstance(item, Symbol):
            self.functions.append(item)
        elif category == "class" and isinstance(item, Symbol):
            self.classes.append(item)
        elif category == "import" and isinstance(item, ImportRef):
            self.imports.append(item)

    def to_symbols(self) -> FileSymbols:
        return FileSymbols(
            functions=tuple(sorted(self.functions, key=lambda s: (s.line, s.name))),
            classes=tuple(sorted(self.classes, key=lambda s: (s.line, s.name))),
            imports=tuple(sorted(self.imports, key=lambda i: (i.line, i.module))),
        )


def _tag_path(tag: dict[str, Any], root: Path | None) -> str | None:
    raw = tag.get("path")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        if root is None:
            return _normalize_repo_path(path.as_posix())
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return None
    return _normalize_repo_path(raw)


def _normalize_repo_path(path: str) -> str:
    posix = PurePosixPath(path.replace("\\", "/"))
    parts = [part for part in posix.parts if part not in ("/", ".")]
    return str(PurePosixPath(*parts)) if parts else ""


# --- Running Ctags ------------------------------------------------------------


def probe_ctags(ctags_path: str, *, timeout: float = CTAGS_PROBE_TIMEOUT_SECONDS) -> str | None:
    """Return a short version string if ``ctags_path`` is Universal Ctags with JSON.

    ``None`` means the binary is missing, is not Universal Ctags, or does not
    support ``--output-format=json``. Never raises.
    """
    if not ctags_path or ctags_path.startswith("-"):
        return None
    try:
        version = subprocess.run(
            [ctags_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = version.stdout or version.stderr or ""
    if "Universal Ctags" not in text:
        return None
    try:
        features = subprocess.run(
            [ctags_path, "--list-features"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "json" not in (features.stdout or "").lower():
        return None
    first = text.splitlines()[0].strip() if text.strip() else "Universal Ctags"
    return first


def require_ctags(ctags_path: str) -> str:
    """Return the version string, or raise ``CtagsError`` if Ctags cannot be used."""
    version = probe_ctags(ctags_path)
    if version is None:
        raise CtagsError(
            "Universal Ctags is required for indexing but was not found or is not usable "
            f"(RA_CTAGS_PATH={ctags_path!r}). Install it with "
            "`apt install universal-ctags` or `brew install universal-ctags`, and make sure "
            "the binary supports --output-format=json."
        )
    return version


def extract_symbols_for_repository(
    root: Path,
    paths: Sequence[str],
    *,
    ctags_path: str,
    timeout: float = CTAGS_TIMEOUT_SECONDS,
) -> dict[str, FileSymbols]:
    """Run Ctags once over ``paths`` (relative to ``root``) and return symbols by path.

    Raises ``CtagsError`` if the binary is missing or the run fails. Paths that
    Ctags does not tag (for example Markdown or JSON) simply do not appear.
    """
    require_ctags(ctags_path)
    safe_paths = [path for path in paths if path and "\n" not in path and "\0" not in path]
    if not safe_paths:
        return {}

    command = [
        ctags_path,
        "--options=NONE",
        "--output-format=json",
        "--fields=+nKselr",
        "--extras=+r",
        "--extras=-p",
        "--links=no",
        "--sort=no",
        "--map-TypeScript=+.tsx",
        "--map-JavaScript=+.jsx",
        "--map-JavaScript=+.cjs",
        "-L",
        "-",
        "-f",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=root,
            input="\n".join(safe_paths) + "\n",
        )
    except subprocess.TimeoutExpired as exc:
        raise CtagsError(
            f"Universal Ctags timed out after {int(timeout)}s while indexing {root}"
        ) from exc
    except OSError as exc:
        raise CtagsError(
            f"Universal Ctags could not be started (RA_CTAGS_PATH={ctags_path!r}): {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [f"exit {result.returncode}"]
        raise CtagsError(f"Universal Ctags failed while indexing {root}: {detail[0]}")
    return parse_ctags_json_lines(result.stdout.splitlines(), root=root)
