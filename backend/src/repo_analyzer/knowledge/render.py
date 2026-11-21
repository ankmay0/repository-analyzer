"""Render a repository map as compact text for a model to read.

The stored map is JSON because programs query it. A model reads an indented
outline more cheaply and more reliably than nested JSON, so that is what the
tools hand over. If the requested part of the map is too large for the budget,
detail is dropped in steps rather than truncating mid-list.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

FileEntry = dict[str, Any]


def render_map(files: Sequence[FileEntry], *, max_chars: int) -> str:
    """Render ``files`` at the most detailed level that fits in ``max_chars``."""
    levels = (
        ("", _render_full),
        (
            "Showing classes and functions only (methods and descriptions omitted to fit). "
            "Pass path_prefix for full detail on one directory.\n\n",
            _render_top_level,
        ),
        (
            "Showing files only (too many definitions to list). Pass path_prefix to see "
            "the definitions in one directory.\n\n",
            _render_files,
        ),
    )
    text = ""
    for note, renderer in levels:
        text = note + "\n".join(renderer(files))
        if len(text) <= max_chars:
            return text
    clipped = text[:max_chars].rsplit("\n", 1)[0]
    return f"{clipped}\n… (list cut short; pass a narrower path_prefix)"


def _visible(entry: FileEntry) -> list[dict[str, Any]]:
    return [s for s in entry["symbols"] if s["kind"] != "variable"]


def _render_full(files: Sequence[FileEntry]) -> list[str]:
    lines = []
    for entry in files:
        lines.append(f"{entry['path']}  ({entry['lines']} lines)")
        for symbol in _visible(entry):
            indent = "    " if symbol["kind"] == "method" else "  "
            doc = f"  — {symbol['doc']}" if symbol["doc"] else ""
            lines.append(f"{indent}{symbol['signature']}  :{symbol['line']}{doc}")
    return lines


def _render_top_level(files: Sequence[FileEntry]) -> list[str]:
    lines = []
    for entry in files:
        lines.append(f"{entry['path']}  ({entry['lines']} lines)")
        lines += [
            f"  {s['signature']}  :{s['line']}" for s in _visible(entry) if s["kind"] != "method"
        ]
    return lines


def _render_files(files: Sequence[FileEntry]) -> list[str]:
    return [
        f"{entry['path']}  ({entry['lines']} lines, {len(_visible(entry))} definitions)"
        for entry in files
    ]
