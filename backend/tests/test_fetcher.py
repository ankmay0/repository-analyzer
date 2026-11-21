from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from repo_analyzer.errors import InvalidRepositorySourceError
from repo_analyzer.ingestion.fetcher import RepoFetcher, parse_remote_url, remove_tree

HOSTS = ("github.com",)


@pytest.mark.parametrize(
    ("source", "expected_url", "expected_name"),
    [
        ("https://github.com/pallets/flask", "https://github.com/pallets/flask", "flask"),
        ("https://github.com/pallets/flask.git", "https://github.com/pallets/flask.git", "flask"),
        ("  https://GitHub.com/pallets/flask/ ", "https://github.com/pallets/flask", "flask"),
    ],
)
def test_accepts_plain_https_urls(source: str, expected_url: str, expected_name: str) -> None:
    assert parse_remote_url(source, HOSTS) == (expected_url, expected_name)


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/pallets/flask",  # not https
        "ssh://git@github.com/pallets/flask",
        "file:///etc/passwd",
        "ext::sh -c 'touch /tmp/pwned'",
        "https://user:token@github.com/pallets/flask",  # embedded credentials
        "https://github.com:8443/pallets/flask",
        "https://evil.example.com/pallets/flask",  # host not allow-listed
        "https://github.com.evil.example.com/pallets/flask",
        "https://github.com/flask",  # no owner
        "https://github.com/pallets/--upload-pack=touch",  # option injection
        "https://github.com/pallets/../../etc",
        "https://github.com/pallets/flask?ref=main",
        "https://github.com/pallets/flask#readme",
    ],
)
def test_rejects_unsafe_or_malformed_urls(source: str) -> None:
    with pytest.raises(InvalidRepositorySourceError):
        parse_remote_url(source, HOSTS)


def test_local_paths_are_rejected_unless_enabled(tmp_path: Path) -> None:
    fetcher = RepoFetcher(tmp_path / "repos", allowed_hosts=HOSTS, allow_local_paths=False)
    with pytest.raises(InvalidRepositorySourceError, match="Local paths are disabled"):
        fetcher.fetch(str(tmp_path))


def test_local_path_is_used_in_place(tmp_path: Path) -> None:
    source = tmp_path / "my-repo"
    source.mkdir()
    fetcher = RepoFetcher(tmp_path / "repos", allowed_hosts=HOSTS, allow_local_paths=True)

    snapshot = fetcher.fetch(str(source))

    assert snapshot.name == "my-repo"
    assert snapshot.root == source.resolve()
    assert snapshot.commit is None  # not a git checkout


def test_remove_tree_deletes_read_only_files_as_found_in_git_checkouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = tmp_path / "repo" / ".git" / "objects" / "pack" / "pack-1.idx"
    pack.parent.mkdir(parents=True)
    pack.write_text("data")
    pack.chmod(stat.S_IREAD)

    # Reproduce Windows, where deleting a read-only file is refused.
    real_unlink = os.unlink

    def windows_like_unlink(path: str, *args: object, **kwargs: object) -> None:
        if not os.stat(path, **kwargs).st_mode & stat.S_IWRITE:  # type: ignore[arg-type]
            raise PermissionError(5, "Access is denied", str(path))
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", windows_like_unlink)

    remove_tree(tmp_path / "repo")

    assert not (tmp_path / "repo").exists()


def test_remove_tree_can_ignore_a_missing_directory(tmp_path: Path) -> None:
    remove_tree(tmp_path / "never-existed", ignore_errors=True)
    with pytest.raises(FileNotFoundError):
        remove_tree(tmp_path / "never-existed")
