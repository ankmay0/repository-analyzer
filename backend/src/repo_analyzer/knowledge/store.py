"""Read and write the JSON knowledge artifact on disk.

The knowledge directory holds exactly one file, ``graph.json``: the workspace
graph of repositories, files and symbols and how they connect.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from repo_analyzer.errors import ReindexRequiredError

Document = dict[str, Any]
_GRAPH_FILE = "graph.json"


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

    def save_graph(self, graph: Document) -> None:
        self._write(self._directory / _GRAPH_FILE, graph)

    def prune(self) -> None:
        """Remove anything in the directory other than the graph.

        A directory written by an earlier version held per-repository pack.json
        and map.json files; this clears them on the next index so nothing stale
        is left behind.
        """
        if not self._directory.is_dir():
            return
        for child in self._directory.iterdir():
            if child.name == _GRAPH_FILE:
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    # -- reading ---------------------------------------------------------------

    def graph(self) -> Document:
        return self._read(self._directory / _GRAPH_FILE)

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
