from __future__ import annotations

import json

import pytest

from repo_analyzer.domain import DependencyScope, Ecosystem, Manifest, SourceFile
from repo_analyzer.ingestion.manifests import normalize_pypi_name, parse_manifest


def parse(path: str, text: str) -> Manifest | None:
    return parse_manifest(SourceFile(repo="r", path=path, language="x", text=text))


def requirements(manifest: Manifest | None) -> set[tuple[str, DependencyScope]]:
    assert manifest is not None
    return {(r.package.name, r.scope) for r in manifest.requires}


def test_pypi_names_are_normalised() -> None:
    assert normalize_pypi_name("Flask_SQLAlchemy.ext") == "flask-sqlalchemy-ext"


def test_package_json() -> None:
    manifest = parse(
        "package.json",
        json.dumps(
            {
                "name": "@scope/app",
                "dependencies": {"express": "^4"},
                "peerDependencies": {"react": "*"},
                "optionalDependencies": {"fsevents": "*"},
                "devDependencies": {"jest": "*"},
            }
        ),
    )
    assert manifest is not None
    assert manifest.provides is not None
    assert (manifest.provides.ecosystem, manifest.provides.name) == (Ecosystem.NPM, "@scope/app")
    assert requirements(manifest) == {
        ("express", DependencyScope.RUNTIME),
        ("react", DependencyScope.RUNTIME),
        ("fsevents", DependencyScope.OPTIONAL),
        ("jest", DependencyScope.DEV),
    }


def test_pyproject_pep621_with_extras_and_dependency_groups() -> None:
    manifest = parse(
        "pyproject.toml",
        """
[project]
name = "My_App"
dependencies = ["Jinja2>=3.1", "MarkupSafe >= 2.0 ; python_version > '3.8'", "click[extra]"]

[project.optional-dependencies]
async = ["asgiref>=3.2"]

[dependency-groups]
dev = ["pytest", {include-group = "other"}]
""",
    )
    assert manifest is not None
    assert manifest.provides is not None
    assert manifest.provides.name == "my-app"
    assert requirements(manifest) == {
        ("jinja2", DependencyScope.RUNTIME),
        ("markupsafe", DependencyScope.RUNTIME),
        ("click", DependencyScope.RUNTIME),
        ("asgiref", DependencyScope.OPTIONAL),
        ("pytest", DependencyScope.DEV),
    }


def test_pyproject_poetry() -> None:
    manifest = parse(
        "pyproject.toml",
        """
[tool.poetry]
name = "svc"

[tool.poetry.dependencies]
python = "^3.11"
requests = "^2"

[tool.poetry.group.dev.dependencies]
pytest = "^8"
""",
    )
    assert manifest is not None and manifest.provides is not None
    assert manifest.provides.name == "svc"
    assert requirements(manifest) == {
        ("requests", DependencyScope.RUNTIME),
        ("pytest", DependencyScope.DEV),
    }


def test_tool_only_pyproject_is_not_a_manifest() -> None:
    assert parse("pyproject.toml", "[tool.ruff]\nline-length = 100\n") is None


def test_setup_cfg() -> None:
    manifest = parse(
        "setup.cfg",
        "[metadata]\nname = legacy\n\n[options]\ninstall_requires =\n    six\n    attrs>=21\n"
        "\n[options.extras_require]\ntest =\n    pytest\n",
    )
    assert manifest is not None and manifest.provides is not None
    assert manifest.provides.name == "legacy"
    assert requirements(manifest) == {
        ("six", DependencyScope.RUNTIME),
        ("attrs", DependencyScope.RUNTIME),
        ("pytest", DependencyScope.OPTIONAL),
    }


@pytest.mark.parametrize(
    ("path", "scope"),
    [
        ("requirements.txt", DependencyScope.RUNTIME),
        ("services/api/requirements.txt", DependencyScope.RUNTIME),
        ("requirements-dev.txt", DependencyScope.DEV),
        ("requirements/tests.txt", DependencyScope.DEV),
    ],
)
def test_requirements_txt_scope_depends_on_the_file_name(path: str, scope: DependencyScope) -> None:
    manifest = parse(path, "# comment\n-r base.txt\nflask==3.0  # pinned\n\ngit+https://x/y.git\n")
    assert requirements(manifest) == {("flask", scope)}


def test_go_mod() -> None:
    manifest = parse(
        "go.mod",
        """module github.com/acme/api

go 1.22

require github.com/acme/shared v1.2.3

require (
	github.com/gorilla/mux v1.8.0
	golang.org/x/sync v0.7.0 // indirect
)
""",
    )
    assert manifest is not None and manifest.provides is not None
    assert manifest.provides.name == "github.com/acme/api"
    assert {name for name, _ in requirements(manifest)} == {
        "github.com/acme/shared",
        "github.com/gorilla/mux",
        "golang.org/x/sync",
    }


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("package.json", "{not json"),
        ("package.json", "[1, 2, 3]"),
        ("pyproject.toml", "[project\nname ="),
        ("setup.cfg", "no section header\n"),
    ],
)
def test_malformed_manifests_are_ignored_not_fatal(path: str, text: str) -> None:
    manifest = parse(path, text)
    assert manifest is None or (manifest.provides is None and not manifest.requires)


def test_non_manifest_files_are_ignored() -> None:
    assert parse("src/app.py", "import os\n") is None
    assert parse("notes.txt", "flask\n") is None


@pytest.mark.parametrize("path", ["examples/celery/requirements.txt", "tests/app/package.json"])
def test_manifests_of_sample_and_test_projects_only_yield_dev_dependencies(path: str) -> None:
    text = "flask==3.0\n" if path.endswith(".txt") else json.dumps({"dependencies": {"flask": "*"}})
    assert requirements(parse(path, text)) == {("flask", DependencyScope.DEV)}
