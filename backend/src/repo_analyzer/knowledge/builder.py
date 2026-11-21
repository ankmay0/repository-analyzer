"""Build the pack and map documents for one repository."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from repo_analyzer.domain import RepoSnapshot, SourceFile
from repo_analyzer.ingestion.scanner import IGNORED_DIRECTORIES
from repo_analyzer.knowledge import SCHEMA_VERSION
from repo_analyzer.knowledge.outline import FileOutline

_STDLIB = sys.stdlib_module_names


def approx_tokens(text: str) -> int:
    """A rough token estimate (about four characters per token), for sizing only."""
    return (len(text) + 3) // 4


def find_package_dirs(root: Path) -> frozenset[str]:
    """Directories under ``root`` that are Python packages, as POSIX relative paths.

    Read from disk rather than from the scanned files because an empty
    ``__init__.py`` is a package marker but has no content worth indexing.
    """
    packages: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRECTORIES]
        if "__init__.py" in filenames:
            relative = Path(dirpath).relative_to(root).as_posix()
            if relative != ".":
                packages.add(relative)
    return frozenset(packages)


def build_pack(snapshot: RepoSnapshot, files: Sequence[SourceFile]) -> dict[str, Any]:
    """The packed repository: every indexed file with its full text."""
    entries = [
        {
            "path": file.path,
            "language": file.language,
            "lines": file.text.count("\n") + (0 if file.text.endswith("\n") else 1),
            "bytes": len(file.text.encode("utf-8")),
            "approx_tokens": approx_tokens(file.text),
            "content": file.text,
        }
        for file in files
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": snapshot.name,
        "source": snapshot.source,
        "commit": snapshot.commit,
        "totals": {
            "files": len(entries),
            "lines": sum(e["lines"] for e in entries),
            "approx_tokens": sum(e["approx_tokens"] for e in entries),
        },
        "files": entries,
    }


def build_map(snapshot: RepoSnapshot, outlines: Sequence[FileOutline]) -> dict[str, Any]:
    """The repository map: what each file defines, without the bodies."""
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": snapshot.name,
        "commit": snapshot.commit,
        "files": [
            {
                "path": outline.path,
                "language": outline.language,
                "lines": outline.lines,
                "module": outline.module,
                "symbols": [
                    {
                        "name": symbol.name,
                        "qualname": symbol.qualname,
                        "kind": symbol.kind,
                        "signature": symbol.signature,
                        "line": symbol.line,
                        "end_line": symbol.end_line,
                        "doc": symbol.doc,
                    }
                    for symbol in outline.symbols
                ],
                "third_party_imports": _third_party_imports(outline),
            }
            for outline in outlines
        ],
    }


def _third_party_imports(outline: FileOutline) -> list[str]:
    """Top-level packages a file imports that are not part of the standard library.

    Includes packages provided by other indexed repositories; the graph is what
    says which of these resolve to indexed code.
    """
    own_top_level = outline.module.split(".")[0] if outline.module else None
    names = {
        statement.module.split(".")[0]
        for statement in outline.imports
        if statement.level == 0 and statement.module
    }
    return sorted(n for n in names if n not in _STDLIB and n != own_top_level)
