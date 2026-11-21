"""Obtain a local checkout of a repository.

Repository sources arrive from API callers, so they are validated before they
get anywhere near ``git``: only ``https`` URLs on an allow-listed host with a
plain ``/owner/name`` path are accepted. That rules out ``file://``, ``ext::``
and ``ssh`` transports, embedded credentials, and option injection.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from repo_analyzer.domain import RepoSnapshot
from repo_analyzer.errors import InvalidRepositorySourceError, RepositoryFetchError

logger = logging.getLogger(__name__)

_PATH_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def parse_remote_url(source: str, allowed_hosts: tuple[str, ...]) -> tuple[str, str]:
    """Validate a remote URL and return ``(canonical_url, repository_name)``."""
    parts = urlsplit(source.strip())
    if parts.scheme != "https":
        raise InvalidRepositorySourceError(
            f"Only https:// repository URLs are accepted: {source!r}"
        )
    if parts.username or parts.password or parts.port:
        raise InvalidRepositorySourceError("Repository URLs must not contain credentials or a port")
    host = (parts.hostname or "").lower()
    if host not in allowed_hosts:
        raise InvalidRepositorySourceError(
            f"Host {host!r} is not allowed (allowed: {', '.join(allowed_hosts)})"
        )
    if parts.query or parts.fragment:
        raise InvalidRepositorySourceError("Repository URLs must not contain a query or fragment")

    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or not all(_PATH_SEGMENT.match(s) for s in segments):
        raise InvalidRepositorySourceError(
            f"Expected a URL of the form https://{host}/<owner>/<repository>: {source!r}"
        )
    name = segments[-1].removesuffix(".git")
    if not _REPO_NAME.match(name):
        raise InvalidRepositorySourceError(f"Cannot derive a repository name from {source!r}")
    return f"https://{host}/{'/'.join(segments)}", name


class RepoFetcher:
    def __init__(
        self,
        repos_dir: Path,
        *,
        allowed_hosts: tuple[str, ...],
        allow_local_paths: bool = False,
        clone_timeout_seconds: int = 300,
    ) -> None:
        self._repos_dir = repos_dir
        self._allowed_hosts = allowed_hosts
        self._allow_local_paths = allow_local_paths
        self._clone_timeout = clone_timeout_seconds

    def resolve_name(self, source: str) -> str:
        """Return the workspace name ``source`` would get, validating it on the way."""
        if self._is_local(source):
            return self._local_root(source).name
        return parse_remote_url(source, self._allowed_hosts)[1]

    def fetch(self, source: str) -> RepoSnapshot:
        if self._is_local(source):
            root = self._local_root(source)
            return RepoSnapshot(
                name=root.name, source=str(root), root=root, commit=_head_commit(root)
            )

        url, name = parse_remote_url(source, self._allowed_hosts)
        dest = self._repos_dir / name
        self._clone(url, dest)
        return RepoSnapshot(name=name, source=url, root=dest, commit=_head_commit(dest))

    # -- internals -------------------------------------------------------------

    @staticmethod
    def _is_local(source: str) -> bool:
        return "://" not in source

    def _local_root(self, source: str) -> Path:
        if not self._allow_local_paths:
            raise InvalidRepositorySourceError(
                "Local paths are disabled; provide an https:// repository URL"
            )
        root = Path(source).expanduser().resolve()
        if not root.is_dir():
            raise InvalidRepositorySourceError(f"Not a directory: {source!r}")
        if not _REPO_NAME.match(root.name):
            raise InvalidRepositorySourceError(f"Unsupported directory name: {root.name!r}")
        return root

    def _clone(self, url: str, dest: Path) -> None:
        # Always clone fresh: a shallow re-clone of a small repository is cheap and
        # removes any chance of indexing a half-updated working tree.
        self._repos_dir.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            try:
                remove_tree(dest)
            except OSError as exc:
                raise RepositoryFetchError(
                    f"Could not replace the previous checkout at {dest}: {exc}. Close any "
                    "program using that folder, or delete it, and index again."
                ) from exc
        command = [
            "git",
            "clone",
            "--depth",
            "1",
            "--single-branch",
            "--no-tags",
            "--",
            url,
            str(dest),
        ]
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        logger.info("Cloning %s", url)
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=self._clone_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            remove_tree(dest, ignore_errors=True)
            raise RepositoryFetchError(
                f"Cloning {url} timed out after {self._clone_timeout}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            remove_tree(dest, ignore_errors=True)
            detail = (exc.stderr or "").strip().splitlines()[-1:] or ["unknown git error"]
            raise RepositoryFetchError(f"Cloning {url} failed: {detail[0]}") from exc
        except FileNotFoundError as exc:
            raise RepositoryFetchError("git is not installed on the server") from exc


def _make_writable_and_retry(function: Callable[..., Any], path: str, _exc: object) -> None:
    """``rmtree`` error handler: clear the read-only flag, then retry the operation."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def remove_tree(path: Path, *, ignore_errors: bool = False) -> None:
    """Delete a directory tree, including read-only files.

    Git marks the files under ``.git/objects`` read-only. On Windows a read-only
    file cannot be deleted, so a plain ``shutil.rmtree`` of a checkout fails
    there with "Access is denied".
    """
    try:
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_make_writable_and_retry)
        else:
            shutil.rmtree(path, onerror=_make_writable_and_retry)
    except OSError:
        if not ignore_errors:
            raise


def _head_commit(root: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return result.stdout.strip() or None
