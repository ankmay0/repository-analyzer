"""Build and store the per-repository JSON index.

Each indexed repository is described by one JSON file,
``<data_dir>/repos/<owner>__<name>.json``. It holds basic repository info, the file
tree (path, language, size, line count), the dependencies declared in package
manifests, and the functions, classes and imports found in each source file. It
deliberately holds no file contents and no model-written summaries: the agent
reads the real files when it needs the code.

Example (abridged)::

    {
      "schema_version": 1,
      "name": "flask", "owner": "pallets", "url": "https://github.com/pallets/flask",
      "default_branch": "main", "commit": "3f2a...", "indexed_at": "2026-11-22T11:30:00+00:00",
      "stats": {"files": 214, "lines": 31870, "size_bytes": 1084312,
                "languages": {"python": 96, "markdown": 41}},
      "manifests": [{"path": "pyproject.toml", "ecosystem": "pypi", "package": "flask"}],
      "dependencies": [{"name": "jinja2", "ecosystem": "pypi", "kind": "runtime",
                        "manifest": "pyproject.toml"}],
      "files": [{"path": "src/flask/app.py", "language": "python", "size_bytes": 48210,
                 "line_count": 1371,
                 "symbols": {"functions": [{"name": "Flask.run", "line": 612, "end_line": 702}],
                             "classes": [{"name": "Flask", "line": 108, "end_line": 1371}],
                             "imports": [{"module": "os", "line": 1}]}}]
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from repo_analyzer.domain import Manifest, RepoSnapshot, SourceFile
from repo_analyzer.errors import UnknownRepositoryError
from repo_analyzer.ingestion.manifests import parse_manifest
from repo_analyzer.ingestion.symbols import extract_symbols_for_repository

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

RepoIndex = dict[str, Any]

_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def index_filename(owner: str, name: str) -> str:
    """``<owner>__<name>.json`` with anything unusual in either part replaced by ``_``."""
    return f"{_UNSAFE_FILENAME_CHARS.sub('_', owner)}__{_UNSAFE_FILENAME_CHARS.sub('_', name)}.json"


def build_repo_index(
    snapshot: RepoSnapshot,
    files: Iterable[SourceFile],
    *,
    indexed_at: str,
    ctags_path: str = "ctags",
) -> RepoIndex:
    """Describe ``snapshot`` from the files the scanner accepted.

    Runs Universal Ctags once over the checkout to fill in functions, classes
    and imports. Raises ``CtagsError`` if Ctags is missing or the run fails.
    """
    file_list = list(files)
    symbols_by_path = extract_symbols_for_repository(
        snapshot.root, [file.path for file in file_list], ctags_path=ctags_path
    )
    file_entries: list[dict[str, Any]] = []
    manifests: list[Manifest] = []
    languages: Counter[str] = Counter()
    total_lines = 0
    total_bytes = 0

    for file in file_list:
        size_bytes = len(file.text.encode("utf-8"))
        line_count = len(file.text.splitlines())
        entry: dict[str, Any] = {
            "path": file.path,
            "language": file.language,
            "size_bytes": size_bytes,
            "line_count": line_count,
        }
        symbols = symbols_by_path.get(file.path)
        if symbols is not None and not symbols.is_empty():
            entry["symbols"] = symbols.to_dict()
        file_entries.append(entry)
        languages[file.language] += 1
        total_lines += line_count
        total_bytes += size_bytes
        if (manifest := parse_manifest(file)) is not None:
            manifests.append(manifest)

    return {
        "schema_version": SCHEMA_VERSION,
        "name": snapshot.name,
        "owner": snapshot.owner,
        "url": snapshot.source,
        "default_branch": snapshot.default_branch,
        "commit": snapshot.commit,
        "indexed_at": indexed_at,
        "stats": {
            "files": len(file_entries),
            "lines": total_lines,
            "size_bytes": total_bytes,
            "languages": dict(sorted(languages.items(), key=lambda item: (-item[1], item[0]))),
        },
        "manifests": [
            {
                "path": m.path,
                "ecosystem": m.provides.ecosystem.value if m.provides else None,
                "package": m.provides.name if m.provides else None,
            }
            for m in manifests
        ],
        "dependencies": [
            {
                "name": r.package.name,
                "ecosystem": r.package.ecosystem.value,
                "kind": r.scope.value,
                "manifest": m.path,
            }
            for m in manifests
            for r in m.requires
        ],
        "files": file_entries,
    }


def symbol_count(index: RepoIndex) -> int:
    """Number of functions and classes recorded across all files."""
    return sum(
        len(file.get("symbols", {}).get("functions", []))
        + len(file.get("symbols", {}).get("classes", []))
        for file in index["files"]
    )


class RepoIndexStore:
    """Reads and writes the JSON index files in one directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def path_for(self, filename: str) -> Path:
        return self._directory / filename

    def save(self, index: RepoIndex) -> str:
        """Write ``index`` atomically and return its file name."""
        filename = index_filename(index["owner"], index["name"])
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._directory / filename
        # Write-then-rename so a crash mid-write cannot leave a truncated file.
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)
        return filename

    def load(self, filename: str) -> RepoIndex:
        try:
            data = json.loads(self.path_for(filename).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnknownRepositoryError(
                f"The index file {filename} is missing or unreadable; re-index the repositories"
            ) from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise UnknownRepositoryError(
                f"The index file {filename} has an unsupported format; re-index the repositories"
            )
        return data

    def delete(self, filename: str) -> None:
        try:
            self.path_for(filename).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not delete %s: %s", filename, exc)
