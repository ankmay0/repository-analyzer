"""Extract functions, classes and imports (with line numbers) from source text.

Python is parsed with the standard library ``ast`` module, so it is exact. Every
other language uses small per-line regular expressions. Those are heuristics:
they are fast, need no parser dependencies, and are right for ordinary code, but
they can miss unusual formatting (for example a function declared across several
lines before its name) and can be fooled by code inside strings or comments.
Treat the output as a map of where to look, not as a compiler's view.

Everything here is a pure function of the file text.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    line: int  # 1-based
    end_line: int | None = None  # only known when parsed with ``ast``

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


# --- Python (ast) -------------------------------------------------------------


class _PythonVisitor(ast.NodeVisitor):
    """Collect top-level functions, classes and methods; nested helpers are skipped."""

    def __init__(self) -> None:
        self.functions: list[Symbol] = []
        self.classes: list[Symbol] = []
        self.imports: list[ImportRef] = []
        self._classes: list[str] = []  # enclosing class names, so methods read ``Class.method``
        self._function_depth = 0

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if self._function_depth == 0:
            self.classes.append(Symbol(".".join([*self._classes, node.name]), *_span(node)))
        self._classes.append(node.name)
        self.generic_visit(node)
        self._classes.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        if self._function_depth == 0:
            self.functions.append(Symbol(".".join([*self._classes, node.name]), *_span(node)))
        self._function_depth += 1
        self.generic_visit(node)
        self._function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports += [ImportRef(alias.name, node.lineno) for alias in node.names]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(ImportRef("." * node.level + (node.module or ""), node.lineno))


def _span(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[int, int | None]:
    return node.lineno, node.end_lineno


def _extract_python(text: str) -> FileSymbols | None:
    """Parse with ``ast``; ``None`` when the file is not valid Python 3."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    visitor = _PythonVisitor()
    visitor.visit(tree)
    return FileSymbols(
        functions=tuple(visitor.functions),
        classes=tuple(visitor.classes),
        imports=tuple(visitor.imports),
    )


# --- Regex heuristics ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Patterns:
    functions: tuple[re.Pattern[str], ...] = ()
    classes: tuple[re.Pattern[str], ...] = ()
    imports: tuple[re.Pattern[str], ...] = ()


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


_IDENT = r"[A-Za-z_$][\w$]*"

_JAVASCRIPT = _Patterns(
    functions=_rx(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*({_IDENT})\s*[<(]",
        rf"^\s*(?:export\s+)?(?:const|let|var)\s+({_IDENT})\s*(?::[^=]+)?=\s*"
        rf"(?:async\s+)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>|{_IDENT}\s*=>)",
    ),
    classes=_rx(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+({_IDENT})",
        rf"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|enum)\s+({_IDENT})",
    ),
    imports=_rx(
        r"""^\s*import\s+(?:type\s+)?['"]([^'"]+)['"]""",
        r"""^\s*(?:import|export)\b[^'"]*?\bfrom\s*['"]([^'"]+)['"]""",
        r"""^\s*\}\s*from\s*['"]([^'"]+)['"]""",  # closing line of a multi-line import
        r"""\brequire\(\s*['"]([^'"]+)['"]\s*\)""",
    ),
)

_PYTHON_FALLBACK = _Patterns(
    functions=_rx(r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
    classes=_rx(r"^\s*class\s+(\w+)"),
    imports=_rx(r"^\s*import\s+([\w.]+)", r"^\s*from\s+(\.*[\w.]*)\s+import\b"),
)

_GO = _Patterns(
    functions=_rx(
        r"^func\s+\(\s*(?:\w+\s+)?\*?\s*(\w+)(?:\[[^\]]*\])?\s*\)\s*(\w+)\s*[(\[]",
        r"^func\s+(\w+)\s*[(\[]",
    ),
    classes=_rx(r"^type\s+(\w+)(?:\[[^\]]*\])?\s+(?:struct|interface)\b"),
    imports=_rx(r"""^import\s+(?:[\w.]+\s+)?"([^"]+)\""""),
)
_GO_BLOCK_IMPORT = re.compile(r"""^\s*(?:[\w.]+\s+)?"([^"]+)"\s*(?://.*)?$""")

_RUST = _Patterns(
    functions=_rx(
        r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?fn\s+(\w+)"
    ),
    classes=_rx(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|union)\s+(\w+)"),
    imports=_rx(r"^\s*(?:pub\s+)?use\s+([\w:]+)", r"^\s*(?:pub\s+)?extern\s+crate\s+(\w+)"),
)

_JAVA_LIKE = _Patterns(
    functions=_rx(
        r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|override|open|"
        r"suspend|async|synchronized)\s+)*fun\s+(?:<[^>]+>\s+)?(?:[\w.]+\.)?(\w+)\s*\(",  # Kotlin
        r"^\s*(?:@\w+\s+)*(?:(?:public|private|protected|internal|static|final|abstract|"
        r"synchronized|virtual|override|async)\s+)+[\w<>\[\],.?\s]+?\s+(\w+)\s*\([^;]*$",
    ),
    classes=_rx(
        r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|open|data|"
        r"partial)\s+)*(?:class|interface|enum|record|object|struct)\s+(\w+)"
    ),
    imports=_rx(r"^\s*import\s+(?:static\s+)?([\w.*]+)", r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;"),
)

_RUBY = _Patterns(
    functions=_rx(r"^\s*def\s+(?:self\.)?([\w?!=]+)"),
    classes=_rx(r"^\s*(?:class|module)\s+([\w:]+)"),
    imports=_rx(r"""^\s*(?:require|require_relative|load)\s*\(?\s*['"]([^'"]+)['"]"""),
)

_PHP = _Patterns(
    functions=_rx(
        r"^\s*(?:(?:public|private|protected|static|final|abstract)\s+)*function\s+&?(\w+)"
    ),
    classes=_rx(r"^\s*(?:(?:abstract|final)\s+)?(?:class|interface|trait|enum)\s+(\w+)"),
    imports=_rx(
        r"^\s*use\s+([\w\\]+)",
        r"""^\s*(?:require|require_once|include|include_once)\s*\(?\s*['"]([^'"]+)['"]""",
    ),
)

_SHELL = _Patterns(
    functions=_rx(r"^\s*function\s+([\w:.-]+)", r"^\s*([\w:.-]+)\s*\(\)\s*\{?"),
    imports=_rx(r"""^\s*(?:source|\.)\s+['"]?([^\s'"]+)"""),
)

_C_LIKE = _Patterns(
    classes=_rx(r"^\s*(?:typedef\s+)?(?:class|struct|enum|union)\s+(\w+)\s*(?:[:{]|$)"),
    imports=_rx(r"""^\s*#\s*include\s*[<"]([^>"]+)[>"]"""),
)

_PROTOBUF = _Patterns(
    functions=_rx(r"^\s*rpc\s+(\w+)\s*\("),
    classes=_rx(r"^\s*(?:message|service|enum)\s+(\w+)"),
    imports=_rx(r"""^\s*import\s+(?:public\s+)?"([^"]+)\""""),
)

_PATTERNS_BY_LANGUAGE: dict[str, _Patterns] = {
    "javascript": _JAVASCRIPT,
    "typescript": _JAVASCRIPT,
    "go": _GO,
    "rust": _RUST,
    "java": _JAVA_LIKE,
    "kotlin": _JAVA_LIKE,
    "csharp": _JAVA_LIKE,
    "ruby": _RUBY,
    "php": _PHP,
    "shell": _SHELL,
    "c": _C_LIKE,
    "cpp": _C_LIKE,
    "protobuf": _PROTOBUF,
}

_HASH_COMMENT_LANGUAGES = frozenset({"python", "shell", "ruby", "php"})
_MAX_LINE_CHARS = 500  # longer lines are minified or generated; matching them is wasteful


def _symbol_name(match: re.Match[str]) -> str:
    return ".".join(g for g in match.groups() if g)


def _extract_with_patterns(text: str, patterns: _Patterns, *, language: str) -> FileSymbols:
    functions: list[Symbol] = []
    classes: list[Symbol] = []
    imports: list[ImportRef] = []
    in_go_import_block = False
    for number, raw in enumerate(text.splitlines(), start=1):
        if len(raw) > _MAX_LINE_CHARS:
            continue
        line = raw.rstrip()
        stripped = line.lstrip()
        if language == "go":
            if in_go_import_block:
                if stripped.startswith(")"):
                    in_go_import_block = False
                elif match := _GO_BLOCK_IMPORT.match(line):
                    imports.append(ImportRef(match.group(1), number))
                continue
            if re.match(r"^import\s*\(", line):
                in_go_import_block = True
                continue
        if stripped.startswith(("//", "/*", "*")):
            continue  # comment (or the body of a block comment)
        if stripped.startswith("#") and language in _HASH_COMMENT_LANGUAGES:
            continue
        for pattern in patterns.functions:
            if match := pattern.match(line):
                functions.append(Symbol(_symbol_name(match), number))
                break
        for pattern in patterns.classes:
            if match := pattern.match(line):
                classes.append(Symbol(_symbol_name(match), number))
                break
        for pattern in patterns.imports:
            if match := pattern.search(line):
                imports.append(ImportRef(match.group(1), number))
                break
    return FileSymbols(tuple(functions), tuple(classes), tuple(imports))


def extract_symbols(language: str, text: str) -> FileSymbols:
    """Return the functions, classes and imports of ``text``, or empty for other languages."""
    if language == "python":
        parsed = _extract_python(text)
        if parsed is not None:
            return parsed
        return _extract_with_patterns(text, _PYTHON_FALLBACK, language="python")
    patterns = _PATTERNS_BY_LANGUAGE.get(language)
    if patterns is None:
        return FileSymbols()
    return _extract_with_patterns(text, patterns, language=language)
