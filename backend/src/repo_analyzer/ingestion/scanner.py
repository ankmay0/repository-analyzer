"""Walk a checkout and yield the text files worth indexing.

The filter is an allow-list of known source/config/documentation types rather
than a deny-list of binaries: an unknown file type is far more likely to be
noise (or a secret) than something a reader would ask questions about.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from repo_analyzer.domain import SourceFile

logger = logging.getLogger(__name__)

LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".sh": "shell",
    ".sql": "sql",
    ".proto": "protobuf",
    ".graphql": "graphql",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".cfg": "ini",
    ".ini": "ini",
    ".md": "markdown",
    ".rst": "restructuredtext",
    ".txt": "text",
    ".html": "html",
    ".css": "css",
}

LANGUAGE_BY_FILENAME: dict[str, str] = {
    "Dockerfile": "dockerfile",
    "Makefile": "makefile",
    "go.mod": "go-module",
}

IGNORED_DIRECTORIES: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode",
        "node_modules", "bower_components", "vendor",
        ".venv", "venv", "__pycache__", ".tox", ".nox",
        ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "dist", "build", "target", "out", "coverage", ".next",
    }
)  # fmt: skip

IGNORED_FILENAMES: frozenset[str] = frozenset(
    {
        "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
        "poetry.lock", "uv.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "composer.lock",
    }
)  # fmt: skip

# Never index likely credentials, even when the suffix is otherwise allowed.
_SECRET_PREFIXES = (".env", "id_rsa", "id_ed25519", "credentials", "secrets")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")


def language_for(path: PurePosixPath) -> str | None:
    """Return the language label for an indexable path, or ``None`` to skip it."""
    name = path.name
    lowered = name.lower()
    if name in IGNORED_FILENAMES:
        return None
    if lowered.startswith(_SECRET_PREFIXES) or lowered.endswith(_SECRET_SUFFIXES):
        return None
    if lowered.endswith((".min.js", ".min.css", ".map")):
        return None
    return LANGUAGE_BY_FILENAME.get(name) or LANGUAGE_BY_SUFFIX.get(path.suffix.lower())


def scan_repository(repo: str, root: Path, *, max_file_bytes: int) -> Iterator[SourceFile]:
    """Yield indexable files under ``root`` in a deterministic order."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRECTORIES)
        for filename in sorted(filenames):
            absolute = Path(dirpath, filename)
            relative = PurePosixPath(absolute.relative_to(root).as_posix())
            language = language_for(relative)
            if language is None:
                continue
            text = _read_text(absolute, max_file_bytes)
            if text is None:
                continue
            yield SourceFile(repo=repo, path=str(relative), language=language, text=text)


def _read_text(path: Path, max_file_bytes: int) -> str | None:
    """Read a regular UTF-8 text file, or return ``None`` if it should be skipped."""
    try:
        # Symlinks are skipped outright: they can point outside the checkout.
        if path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size > max_file_bytes:
            logger.debug("Skipping %s: larger than %d bytes", path, max_file_bytes)
            return None
        raw = path.read_bytes()
    except OSError as exc:
        logger.warning("Skipping %s: %s", path, exc)
        return None
    if b"\x00" in raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text if text.strip() else None
