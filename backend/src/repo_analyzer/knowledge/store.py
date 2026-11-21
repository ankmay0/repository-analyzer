"""Read and write the JSON knowledge artifacts on disk.

Layout under the knowledge directory::

    graph.json           the workspace graph
    <repo>/pack.json     the packed repository
    <repo>/map.json      the repository map
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from repo_analyzer.errors import ReindexRequiredError

Document = dict[str, Any]


class KnowledgeStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()
        # Parsed documents, keyed by path and invalidated when the file changes.
        self._cache: dict[Path, tuple[int, Document]] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    # -- writing ---------------------------------------------------------------

    def save_repository(self, repo: str, pack: Document, repo_map: Document) -> None:
        self._write(self._directory / repo / "pack.json", pack)
        self._write(self._directory / repo / "map.json", repo_map)

    def save_graph(self, graph: Document) -> None:
        self._write(self._directory / "graph.json", graph)

    def prune(self, keep: Iterable[str]) -> None:
        """Delete artifacts of repositories that are no longer in the workspace."""
        wanted = set(keep)
        if not self._directory.is_dir():
            return
        for child in self._directory.iterdir():
            if child.is_dir() and child.name not in wanted:
                shutil.rmtree(child)

    # -- reading ---------------------------------------------------------------

    def pack(self, repo: str) -> Document:
        return self._read(self._directory / repo / "pack.json")

    def map(self, repo: str) -> Document:
        return self._read(self._directory / repo / "map.json")

    def graph(self) -> Document:
        return self._read(self._directory / "graph.json")

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _write(path: Path, document: Document) -> None:
        # Write-then-rename so a reader never sees a half-written file.
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)

    def _read(self, path: Path) -> Document:
        try:
            modified = path.stat().st_mtime_ns
        except FileNotFoundError:
            raise ReindexRequiredError(
                f"No knowledge file at {path.name} for this workspace; index the repositories again"
            ) from None
        with self._lock:
            cached = self._cache.get(path)
            if cached is not None and cached[0] == modified:
                return cached[1]
        document: Document = json.loads(path.read_text(encoding="utf-8"))
        with self._lock:
            self._cache[path] = (modified, document)
        return document
