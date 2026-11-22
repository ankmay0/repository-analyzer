"""Parse package manifests into what a repository *provides* and *requires*.

The dependencies in a repository's JSON index come from declared manifests,
never from the language model. Each parser is a pure
function of ``(repo, path, text)`` and returns ``None`` when the file is
malformed, so one bad manifest cannot fail an indexing run.

Package names are normalised per ecosystem so that a requirement and the
package it refers to compare equal (PyPI: PEP 503; npm and Go: case-sensitive,
used verbatim).
"""

from __future__ import annotations

import configparser
import json
import logging
import re
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from repo_analyzer.domain import (
    DependencyScope,
    Ecosystem,
    Manifest,
    PackageRef,
    Requirement,
    SourceFile,
)

logger = logging.getLogger(__name__)

Parser = Callable[[str, str, str], Manifest | None]

# Manifests under these directories describe sample or test projects, not what the
# repository ships, so whatever they require is at most a development dependency.
_NON_SHIPPING_DIRECTORIES = frozenset(
    {"examples", "example", "samples", "demo", "docs", "doc", "tests", "test", "fixtures"}
)

_PEP508_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GO_REQUIRE_LINE = re.compile(r"^\s*([^\s/]+\.[^\s]+)\s+v\S+")


def normalize_pypi_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _pypi(name: str) -> PackageRef:
    return PackageRef(Ecosystem.PYPI, normalize_pypi_name(name))


def _pypi_requirements(specs: Iterable[object], scope: DependencyScope) -> list[Requirement]:
    """Extract package names from PEP 508 strings, ignoring anything else."""
    requirements = []
    for spec in specs:
        if isinstance(spec, str) and (match := _PEP508_NAME.match(spec)):
            requirements.append(Requirement(_pypi(match.group(1)), scope))
    return requirements


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


# --- npm ----------------------------------------------------------------------


def parse_package_json(repo: str, path: str, text: str) -> Manifest | None:
    try:
        data = _as_dict(json.loads(text))
    except json.JSONDecodeError:
        return None
    name = data.get("name")
    scopes = {
        "dependencies": DependencyScope.RUNTIME,
        "peerDependencies": DependencyScope.RUNTIME,
        "optionalDependencies": DependencyScope.OPTIONAL,
        "devDependencies": DependencyScope.DEV,
    }
    requires = [
        Requirement(PackageRef(Ecosystem.NPM, dependency), scope)
        for field, scope in scopes.items()
        for dependency in _as_dict(data.get(field))
    ]
    provides = PackageRef(Ecosystem.NPM, name) if isinstance(name, str) and name else None
    return Manifest(repo=repo, path=path, provides=provides, requires=tuple(requires))


# --- PyPI ---------------------------------------------------------------------


def parse_pyproject_toml(repo: str, path: str, text: str) -> Manifest | None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    project = _as_dict(data.get("project"))
    poetry = _as_dict(_as_dict(data.get("tool")).get("poetry"))

    requires = _pypi_requirements(_as_list(project.get("dependencies")), DependencyScope.RUNTIME)
    for extras in _as_dict(project.get("optional-dependencies")).values():
        requires += _pypi_requirements(_as_list(extras), DependencyScope.OPTIONAL)
    for group in _as_dict(data.get("dependency-groups")).values():  # PEP 735
        requires += _pypi_requirements(_as_list(group), DependencyScope.DEV)

    requires += [
        Requirement(_pypi(dependency), DependencyScope.RUNTIME)
        for dependency in _as_dict(poetry.get("dependencies"))
        if dependency.lower() != "python"
    ]
    for group in _as_dict(poetry.get("group")).values():
        requires += [
            Requirement(_pypi(dependency), DependencyScope.DEV)
            for dependency in _as_dict(_as_dict(group).get("dependencies"))
        ]

    name = project.get("name") or poetry.get("name")
    provides = _pypi(name) if isinstance(name, str) and name else None
    if provides is None and not requires:
        return None  # a pyproject.toml that only configures tools
    return Manifest(repo=repo, path=path, provides=provides, requires=tuple(requires))


def parse_setup_cfg(repo: str, path: str, text: str) -> Manifest | None:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    name = parser.get("metadata", "name", fallback="").strip()
    install_requires = parser.get("options", "install_requires", fallback="")
    requires = _pypi_requirements(install_requires.splitlines(), DependencyScope.RUNTIME)
    if parser.has_section("options.extras_require"):
        for _, extras in parser.items("options.extras_require"):
            requires += _pypi_requirements(extras.splitlines(), DependencyScope.OPTIONAL)
    provides = _pypi(name) if name else None
    if provides is None and not requires:
        return None
    return Manifest(repo=repo, path=path, provides=provides, requires=tuple(requires))


def parse_requirements_txt(repo: str, path: str, text: str) -> Manifest | None:
    # Only a top-level-style ``requirements.txt`` is treated as a runtime contract;
    # ``requirements-dev.txt``, ``requirements/tests.txt`` etc. are tooling pins.
    is_runtime = PurePosixPath(path).name == "requirements.txt"
    scope = DependencyScope.RUNTIME if is_runtime else DependencyScope.DEV
    lines = (line.split("#", 1)[0].strip() for line in text.splitlines())
    specs = [line for line in lines if line and not line.startswith(("-", "git+", "http"))]
    requires = _pypi_requirements(specs, scope)
    if not requires:
        return None
    return Manifest(repo=repo, path=path, provides=None, requires=tuple(requires))


# --- Go -----------------------------------------------------------------------


def parse_go_mod(repo: str, path: str, text: str) -> Manifest | None:
    module: str | None = None
    requires: list[Requirement] = []
    in_require_block = False
    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if line.startswith("module "):
            module = line.removeprefix("module ").strip().strip('"')
        elif line.startswith("require ("):
            in_require_block = True
        elif in_require_block and line == ")":
            in_require_block = False
        elif in_require_block or line.startswith("require "):
            match = _GO_REQUIRE_LINE.match(line.removeprefix("require "))
            if match:
                requires.append(
                    Requirement(PackageRef(Ecosystem.GO, match.group(1)), DependencyScope.RUNTIME)
                )
    provides = PackageRef(Ecosystem.GO, module) if module else None
    return Manifest(repo=repo, path=path, provides=provides, requires=tuple(requires))


# --- dispatch -----------------------------------------------------------------

_PARSERS_BY_FILENAME: dict[str, Parser] = {
    "package.json": parse_package_json,
    "pyproject.toml": parse_pyproject_toml,
    "setup.cfg": parse_setup_cfg,
    "go.mod": parse_go_mod,
}


def _parser_for(path: str) -> Parser | None:
    pure = PurePosixPath(path)
    if parser := _PARSERS_BY_FILENAME.get(pure.name):
        return parser
    is_requirements = pure.suffix == ".txt" and (
        pure.name.startswith("requirements") or pure.parent.name == "requirements"
    )
    return parse_requirements_txt if is_requirements else None


def parse_manifest(file: SourceFile) -> Manifest | None:
    """Parse ``file`` if it is a recognised manifest; otherwise return ``None``."""
    parser = _parser_for(file.path)
    if parser is None:
        return None
    manifest = parser(file.repo, file.path, file.text)
    if manifest is None:
        logger.warning("Ignoring unparseable manifest %s/%s", file.repo, file.path)
        return None
    if _NON_SHIPPING_DIRECTORIES.intersection(PurePosixPath(file.path).parts[:-1]):
        demoted = tuple(replace(r, scope=DependencyScope.DEV) for r in manifest.requires)
        return replace(manifest, requires=demoted)
    return manifest
