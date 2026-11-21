"""Helpers for building the knowledge graph.

The only artifact written in json mode is the workspace graph (see
``repo_analyzer.knowledge.graph``). This module holds the one piece of
filesystem knowledge the graph needs that the scanned files do not carry.
"""

from __future__ import annotations

import os
from pathlib import Path

from repo_analyzer.ingestion.scanner import IGNORED_DIRECTORIES


def find_package_dirs(root: Path) -> frozenset[str]:
    """Directories under ``root`` that are Python packages, as POSIX relative paths.

    Read from disk rather than from the scanned files because an empty
    ``__init__.py`` is a package marker but has no content worth indexing, so it
    is not among the files handed to the outliner.
    """
    packages: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRECTORIES]
        if "__init__.py" in filenames:
            relative = Path(dirpath).relative_to(root).as_posix()
            if relative != ".":
                packages.add(relative)
    return frozenset(packages)
