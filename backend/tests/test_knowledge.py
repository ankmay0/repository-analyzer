"""The JSON knowledge artifact: file outlines, package discovery and the graph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from repo_analyzer.config import Settings
from repo_analyzer.domain import (
    DependencyEdge,
    DependencyScope,
    Ecosystem,
    PackageRef,
    SourceFile,
)
from repo_analyzer.ingestion.dependency_graph import DependencyGraph
from repo_analyzer.knowledge.builder import find_package_dirs
from repo_analyzer.knowledge.graph import build_graph
from repo_analyzer.knowledge.outline import FileOutline, module_name, outline_file
from repo_analyzer.knowledge.store import KnowledgeStore
from repo_analyzer.workspace import Workspace
from support import write_tree

NO_DEPENDENCIES = DependencyGraph((), ())


def outline(repo: str, path: str, text: str, packages: set[str] | None = None) -> FileOutline:
    file = SourceFile(repo=repo, path=path, language="python", text=text)
    return outline_file(file, frozenset(packages or set()))


def edges_of(graph: dict[str, Any], kind: str) -> set[tuple[str, str]]:
    return {(e["source"], e["target"]) for e in graph["edges"] if e["kind"] == kind}


# --- outlines -------------------------------------------------------------------


def test_module_names_follow_the_package_chain() -> None:
    packages = frozenset({"src/flask", "src/flask/json"})

    assert module_name("src/flask/app.py", packages) == ("flask.app", False)
    assert module_name("src/flask/__init__.py", packages) == ("flask", True)
    assert module_name("src/flask/json/tag.py", packages) == ("flask.json.tag", False)
    assert module_name("scripts/run.py", packages) == ("run", False)  # not inside a package
    assert module_name("README.md", packages) is None
    assert module_name("docs/my-notes.py", packages) is None  # not an importable name


def test_outline_lists_classes_functions_methods_and_variables() -> None:
    result = outline(
        "r",
        "pkg/shapes.py",
        '''"""Module docstring."""
import os
from typing import TYPE_CHECKING

LIMIT: int = 10

if TYPE_CHECKING:
    from pkg.other import Other


class Shape(Base, metaclass=Meta):
    """A drawable shape.

    More detail that is not part of the summary.
    """

    def area(self, scale: float = 1.0) -> float:
        """Return the area."""
        return 0.0

    async def load(self):
        import json
        return json


def make(kind: str, *args, **kwargs) -> Shape:
    return Shape()
''',
        {"pkg"},
    )

    assert result.module == "pkg.shapes"
    by_name = {s.qualname: s for s in result.symbols}
    assert set(by_name) == {"LIMIT", "Shape", "Shape.area", "Shape.load", "make"}
    assert by_name["Shape"].kind == "class"
    assert by_name["Shape"].signature == "class Shape(Base)"  # keywords are not bases
    assert by_name["Shape"].doc == "A drawable shape."
    assert (by_name["Shape"].line, by_name["Shape"].end_line) == (11, 23)
    assert by_name["Shape.area"].kind == "method"
    assert by_name["Shape.area"].signature == "def area(self, scale: float=1.0) -> float"
    assert by_name["Shape.load"].signature == "async def load(self)"
    assert by_name["make"].signature == "def make(kind: str, *args, **kwargs) -> Shape"
    assert by_name["LIMIT"].kind == "variable"
    # Imports are collected everywhere, including under TYPE_CHECKING and inside functions.
    assert {(i.module, i.name) for i in result.imports} == {
        ("os", None),
        ("typing", "TYPE_CHECKING"),
        ("pkg.other", "Other"),
        ("json", None),
    }
    assert result.top_level("Shape") is by_name["Shape"]
    assert result.top_level("area") is None  # a method is not a module-level name


def test_unparseable_and_non_python_files_keep_an_empty_outline() -> None:
    broken = outline("r", "old.py", "print 'python 2'\n")
    readme = outline_file(
        SourceFile("r", "README.md", "markdown", "# Title\n\nText\n"), frozenset()
    )

    assert (broken.module, broken.symbols, broken.lines) == ("old", (), 1)
    assert (readme.module, readme.symbols, readme.lines) == (None, (), 3)


# --- graph: import resolution ---------------------------------------------------


def test_imports_resolve_within_a_repository_including_relative_ones() -> None:
    files = [
        outline("app", "pkg/__init__.py", "from .core import Engine\n", {"pkg", "pkg/sub"}),
        outline("app", "pkg/core.py", "class Engine:\n    pass\n", {"pkg", "pkg/sub"}),
        outline(
            "app",
            "pkg/sub/worker.py",
            "from ..core import Engine\nfrom .. import core\n"
            "import pkg.core\nfrom . import missing\n",
            {"pkg", "pkg/sub"},
        ),
    ]

    graph = build_graph({"app": files}, {"app": "abc"}, NO_DEPENDENCIES)

    worker = "file:app/pkg/sub/worker.py"
    assert (worker, "file:app/pkg/core.py") in edges_of(graph, "imports")
    assert (worker, "symbol:app/pkg/core.py#Engine") in edges_of(graph, "uses")
    assert ("file:app/pkg/__init__.py", "symbol:app/pkg/core.py#Engine") in edges_of(graph, "uses")
    assert ("file:app/pkg/core.py", "symbol:app/pkg/core.py#Engine") in edges_of(graph, "defines")
    # One edge per file pair, at the first line that imports it.
    lines = [e["line"] for e in graph["edges"] if e["kind"] == "imports" and e["source"] == worker]
    assert lines == [1]


def test_cross_repository_uses_follow_re_exports_to_the_definition() -> None:
    lib = [
        outline("lib", "src/lib/__init__.py", "from .impl import Thing as Thing\n", {"src/lib"}),
        outline("lib", "src/lib/impl.py", "class Thing:\n    pass\n", {"src/lib"}),
    ]
    app = [outline("app", "main.py", "import os\n\nfrom lib import Thing\nimport requests\n")]

    graph = build_graph({"lib": lib, "app": app}, {}, NO_DEPENDENCIES)

    # Linked to where Thing is defined, not to the __init__ that re-exports it.
    assert ("file:app/main.py", "symbol:lib/src/lib/impl.py#Thing") in edges_of(graph, "uses")
    assert ("file:app/main.py", "file:lib/src/lib/__init__.py") in edges_of(graph, "imports")
    use = next(
        e for e in graph["edges"] if e["kind"] == "uses" and e["source"] == "file:app/main.py"
    )
    assert (use["line"], use["name"]) == (3, "Thing")
    # Third-party and standard-library imports produce no edges.
    assert len([e for e in graph["edges"] if e["source"] == "file:app/main.py"]) == 2


def test_a_same_named_loose_module_in_another_repository_is_not_linked() -> None:
    other = [outline("other", "utils.py", "def helper():\n    pass\n")]
    app = [outline("app", "main.py", "from utils import helper\n")]

    unrelated = build_graph({"other": other, "app": app}, {}, NO_DEPENDENCIES)
    assert edges_of(unrelated, "imports") == set()

    # ...unless a manifest says app really does depend on that repository.
    declared = DependencyGraph(
        ["app", "other"],
        [
            DependencyEdge(
                "app", "other", PackageRef(Ecosystem.PYPI, "utils"), DependencyScope.RUNTIME, "x"
            )
        ],
    )
    linked = build_graph({"other": other, "app": app}, {}, declared)
    assert ("file:app/main.py", "symbol:other/utils.py#helper") in edges_of(linked, "uses")
    assert ("repo:app", "repo:other") in edges_of(linked, "depends_on")


def test_a_repository_resolves_its_own_module_before_another_repositorys() -> None:
    a = [
        outline("a", "pkg/__init__.py", "X = 1\n", {"pkg"}),
        outline("a", "main.py", "from pkg import X\n"),
    ]
    b = [outline("b", "pkg/__init__.py", "X = 2\n", {"pkg"})]

    graph = build_graph({"a": a, "b": b}, {}, NO_DEPENDENCIES)

    assert edges_of(graph, "uses") == {("file:a/main.py", "symbol:a/pkg/__init__.py#X")}


def test_import_cycles_between_modules_terminate() -> None:
    files = [
        outline("r", "p/__init__.py", "from .a import *\n", {"p"}),
        outline("r", "p/a.py", "from .b import *\n", {"p"}),
        outline("r", "p/b.py", "from .a import *\nfrom . import nothing\n", {"p"}),
        outline("r", "main.py", "from p import Missing\n"),
    ]

    graph = build_graph({"r": files}, {}, NO_DEPENDENCIES)

    assert ("file:r/main.py", "file:r/p/__init__.py") in edges_of(graph, "imports")
    assert not [e for e in graph["edges"] if e["kind"] == "uses"]


# --- pack and map documents -----------------------------------------------------


def test_find_package_dirs_reads_markers_from_disk(tmp_path: Path) -> None:
    root = write_tree(
        tmp_path / "demo",
        {
            "pkg/__init__.py": "",  # an empty marker: not indexed, still a package
            "pkg/sub/__init__.py": "",
            "pkg/mod.py": "x = 1\n",
            "scripts/run.py": "print('hi')\n",  # no __init__, not a package
        },
    )

    assert find_package_dirs(root) == frozenset({"pkg", "pkg/sub"})


# --- end to end: indexing writes only the graph ---------------------------------


def test_indexing_in_json_mode_writes_only_the_graph(
    json_workspace: Workspace, knowledge: KnowledgeStore, json_settings: Settings
) -> None:
    state = json_workspace.state
    assert state.knowledge_mode == "json"
    assert all(r.symbol_count >= 0 and r.chunk_count == 0 for r in state.repositories)
    assert next(r for r in state.repositories if r.name == "core-lib").symbol_count == 3
    assert not json_settings.index_dir.exists()  # no vector store was created

    files = sorted(
        p.relative_to(knowledge.directory).as_posix() for p in knowledge.directory.iterdir()
    )
    assert files == ["graph.json"]  # the one artifact; no per-repository pack or map

    graph = knowledge.graph()
    assert graph["edge_kinds"] == ["depends_on", "defines", "imports", "uses"]
    sign_token = "symbol:core-lib/src/core_lib/tokens.py#sign_token"
    assert edges_of(graph, "uses") >= {
        ("file:mid-lib/src/mid_lib/session.py", sign_token),
        # Followed through core_lib/__init__.py's re-export to the real definition:
        ("file:tools/scripts/smoke.py", sign_token),
        ("file:web-app/app/login.py", "symbol:mid-lib/src/mid_lib/session.py#create_session"),
    }
    assert ("repo:mid-lib", "repo:core-lib") in edges_of(graph, "depends_on")
    # The secret file is never scanned, so it reaches neither the files nor the graph.
    assert "do-not-index" not in json.dumps(graph)


def test_the_graph_is_a_pure_function_of_the_repositories(
    json_settings: Settings, fixture_repos: dict[str, Path], tmp_path: Path
) -> None:
    from conftest import make_indexing

    def index_into(name: str) -> str:
        settings = json_settings.model_copy(update={"data_dir": tmp_path / name})
        store = KnowledgeStore(settings.knowledge_dir)
        make_indexing(settings, Workspace(settings.workspace_file), store, None).run(
            [str(p) for p in fixture_repos.values()]
        )
        return (store.directory / "graph.json").read_text()

    assert index_into("first") == index_into("second")


def test_reindexing_drops_a_removed_repository_and_old_per_repo_files(
    json_settings: Settings,
    json_workspace: Workspace,
    knowledge: KnowledgeStore,
    fixture_repos: dict[str, Path],
) -> None:
    from conftest import make_indexing

    # A directory left by an earlier version must be cleared by prune().
    (knowledge.directory / "mid-lib").mkdir()
    (knowledge.directory / "mid-lib" / "pack.json").write_text("{}")

    make_indexing(json_settings, json_workspace, knowledge, None).run(
        [str(fixture_repos["core-lib"])]
    )

    assert [p.name for p in knowledge.directory.iterdir()] == ["graph.json"]
    assert {n["repo"] for n in knowledge.graph()["nodes"] if n["kind"] == "file"} == {"core-lib"}
