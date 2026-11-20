from __future__ import annotations

from pathlib import Path

from repo_analyzer.ingestion.scanner import scan_repository
from support import write_tree


def scan(root: Path, max_file_bytes: int = 10_000) -> dict[str, str]:
    return {f.path: f.language for f in scan_repository("r", root, max_file_bytes=max_file_bytes)}


def test_indexes_source_and_docs_with_language_labels(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {"src/app.py": "x = 1\n", "README.md": "# hi\n", "Dockerfile": "FROM scratch\n"},
    )
    assert scan(tmp_path) == {
        "Dockerfile": "dockerfile",
        "README.md": "markdown",
        "src/app.py": "python",
    }


def test_skips_dependencies_lockfiles_secrets_and_unknown_types(tmp_path: Path) -> None:
    write_tree(
        tmp_path,
        {
            "keep.py": "x = 1\n",
            "node_modules/pkg/index.js": "module.exports = 1\n",
            ".git/config": "[core]\n",
            "package-lock.json": "{}\n",
            ".env": "TOKEN=abc\n",
            ".env.production": "TOKEN=abc\n",
            "deploy/server.pem": "-----BEGIN PRIVATE KEY-----\n",
            "credentials.json": '{"key": "abc"}\n',
            "bundle.min.js": "var a=1;\n",
            "image.png": "not really a png\n",
            "empty.py": "   \n",
        },
    )
    assert scan(tmp_path) == {"keep.py": "python"}


def test_skips_binary_oversized_and_non_utf8_files(tmp_path: Path) -> None:
    (tmp_path / "binary.py").write_bytes(b"x = 1\x00\x01")
    (tmp_path / "latin1.py").write_bytes("caf\xe9 = 1\n".encode("latin-1"))
    (tmp_path / "big.py").write_text("x = 1\n" * 100)
    (tmp_path / "ok.py").write_text("x = 1\n")
    assert scan(tmp_path, max_file_bytes=100) == {"ok.py": "python"}


def test_does_not_follow_symlinks_out_of_the_repository(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("password = 'hunter2'\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("x = 1\n")
    (repo / "link.py").symlink_to(outside / "secret.py")
    (repo / "linked_dir").symlink_to(outside, target_is_directory=True)

    assert scan(repo) == {"ok.py": "python"}
