from __future__ import annotations

import json
import stat
import textwrap
from pathlib import Path

import pytest

from repo_analyzer.errors import CtagsError
from repo_analyzer.ingestion.symbols import (
    classify_ctags_tag,
    extract_symbols_for_repository,
    parse_ctags_json_lines,
    probe_ctags,
    require_ctags,
)

# --- Canned Ctags JSON (no binary required) -----------------------------------


def _tag(**fields: object) -> str:
    payload = {"_type": "tag", **fields}
    return json.dumps(payload)


def parse(*lines: str):
    return parse_ctags_json_lines(lines)


def names(symbols: tuple) -> list[tuple[str, int]]:  # type: ignore[type-arg]
    return [(s.name, s.line) for s in symbols]


def test_python_functions_classes_methods_and_end_lines() -> None:
    result = parse(
        _tag(
            name="Service",
            path="src/app.py",
            language="Python",
            kind="class",
            line=15,
            end=24,
            roles="def",
        ),
        _tag(
            name="Config",
            path="src/app.py",
            language="Python",
            kind="class",
            line=22,
            end=24,
            scope="Service",
            scopeKind="class",
            roles="def",
        ),
        _tag(
            name="run",
            path="src/app.py",
            language="Python",
            kind="member",
            line=17,
            end=19,
            scope="Service",
            scopeKind="class",
            roles="def",
        ),
        _tag(
            name="validate",
            path="src/app.py",
            language="Python",
            kind="member",
            line=23,
            end=24,
            scope="Service.Config",
            scopeKind="class",
            roles="def",
        ),
        _tag(name="top", path="src/app.py", language="Python", kind="function", line=6, end=10),
        _tag(
            name="inner",
            path="src/app.py",
            language="Python",
            kind="function",
            line=7,
            end=8,
            scope="top",
            scopeKind="function",
            file=True,
        ),
        _tag(
            name="attr",
            path="src/app.py",
            language="Python",
            kind="variable",
            line=16,
            scope="Service",
            scopeKind="class",
        ),
        _tag(name="X", path="src/app.py", language="Python", kind="variable", line=4),
    )

    symbols = result["src/app.py"]
    assert names(symbols.functions) == [
        ("top", 6),
        ("Service.run", 17),
        ("Service.Config.validate", 23),
    ]
    assert [f.end_line for f in symbols.functions] == [10, 19, 24]
    assert names(symbols.classes) == [("Service", 15), ("Service.Config", 22)]
    assert symbols.imports == ()


def test_python_imports_come_from_reference_tags_and_are_deduplicated() -> None:
    result = parse(
        _tag(
            name="os",
            path="imports.py",
            language="Python",
            kind="module",
            line=1,
            roles="imported",
            extras="reference",
        ),
        _tag(
            name="sys",
            path="imports.py",
            language="Python",
            kind="module",
            line=1,
            roles="indirectlyImported",
            extras="reference",
        ),
        _tag(
            name="system",
            path="imports.py",
            language="Python",
            kind="namespace",
            line=1,
            roles="def",
        ),  # alias definition, not an import
        _tag(
            name="pathlib",
            path="imports.py",
            language="Python",
            kind="module",
            line=2,
            roles="namespace",
            extras="reference",
        ),
        _tag(
            name="Path",
            path="imports.py",
            language="Python",
            kind="unknown",
            line=2,
            scope="pathlib",
            scopeKind="module",
            roles="imported",
            extras="reference",
        ),  # same module and line as the namespace tag
        _tag(
            name="core_lib.tokens",
            path="imports.py",
            language="Python",
            kind="module",
            line=5,
            roles="namespace",
            extras="reference",
        ),
        _tag(
            name="sign_token",
            path="imports.py",
            language="Python",
            kind="unknown",
            line=5,
            scope="core_lib.tokens",
            scopeKind="module",
            roles="imported",
            extras="reference",
        ),
        _tag(
            name=".",
            path="imports.py",
            language="Python",
            kind="module",
            line=3,
            roles="namespace",
            extras="reference",
        ),
    )

    imports = [(i.module, i.line) for i in result["imports.py"].imports]
    assert imports == [("os", 1), ("sys", 1), ("pathlib", 2), (".", 3), ("core_lib.tokens", 5)]


def test_go_methods_drop_the_package_prefix_and_struct_fields() -> None:
    result = parse(
        _tag(name="main", path="a.go", language="Go", kind="package", line=1, roles="def"),
        _tag(
            name="fmt",
            path="a.go",
            language="Go",
            kind="package",
            line=2,
            roles="imported",
            extras="reference",
        ),
        _tag(
            name="Server",
            path="a.go",
            language="Go",
            kind="struct",
            line=3,
            end=3,
            scope="main",
            scopeKind="package",
        ),
        _tag(
            name="addr",
            path="a.go",
            language="Go",
            kind="member",
            line=3,
            scope="main.Server",
            scopeKind="struct",
        ),
        _tag(
            name="Start",
            path="a.go",
            language="Go",
            kind="func",
            line=6,
            end=6,
            scope="main.Server",
            scopeKind="struct",
        ),
        _tag(
            name="main",
            path="a.go",
            language="Go",
            kind="func",
            line=5,
            end=5,
            scope="main",
            scopeKind="package",
        ),
    )

    symbols = result["a.go"]
    assert names(symbols.functions) == [("main", 5), ("Server.Start", 6)]
    assert names(symbols.classes) == [("Server", 3)]
    assert [(i.module, i.line) for i in symbols.imports] == [("fmt", 2)]


def test_tsx_keeps_functions_and_classes_and_drops_constants() -> None:
    result = parse(
        _tag(name="load", path="App.tsx", language="TypeScript", kind="function", line=6),
        _tag(name="gen", path="App.tsx", language="TypeScript", kind="generator", line=7),
        _tag(name="handler", path="App.tsx", language="TypeScript", kind="constant", line=8),
        _tag(name="App", path="App.tsx", language="TypeScript", kind="class", line=12),
        _tag(name="Props", path="App.tsx", language="TypeScript", kind="interface", line=21),
        _tag(name="Mode", path="App.tsx", language="TypeScript", kind="enum", line=23),
        _tag(name="Alias", path="App.tsx", language="TypeScript", kind="alias", line=22),
        _tag(
            name="helper",
            path="App.tsx",
            language="TypeScript",
            kind="constant",
            line=27,
            scope="Chat",
            scopeKind="function",
        ),
        _tag(name="Chat", path="App.tsx", language="TypeScript", kind="function", line=25),
        _tag(
            name="a",
            path="App.tsx",
            language="TypeScript",
            kind="property",
            line=21,
            scope="Props",
            scopeKind="interface",
        ),
    )

    symbols = result["App.tsx"]
    assert names(symbols.functions) == [("load", 6), ("gen", 7), ("Chat", 25)]
    assert names(symbols.classes) == [("App", 12), ("Props", 21)]  # interface kept; enum/alias not
    assert symbols.imports == ()  # Ctags does not currently emit JS/TS imports


def test_c_and_rust_fields_are_not_methods() -> None:
    result = parse(
        _tag(name="point", path="a.c", language="C", kind="struct", line=2, end=2),
        _tag(
            name="x",
            path="a.c",
            language="C",
            kind="member",
            line=2,
            scope="point",
            scopeKind="struct",
        ),
        _tag(name="add", path="a.c", language="C", kind="function", line=4, end=4),
        _tag(
            name="stdio.h",
            path="a.c",
            language="C",
            kind="header",
            line=1,
            roles="system",
            extras="reference",
        ),
        _tag(name="A", path="a.rs", language="Rust", kind="struct", line=2),
        _tag(name="A", path="a.rs", language="Rust", kind="implementation", line=5),
        _tag(
            name="new",
            path="a.rs",
            language="Rust",
            kind="method",
            line=6,
            scope="A",
            scopeKind="implementation",
        ),
        _tag(
            name="x",
            path="a.rs",
            language="Rust",
            kind="field",
            line=2,
            scope="A",
            scopeKind="struct",
        ),
    )

    assert names(result["a.c"].functions) == [("add", 4)]
    assert names(result["a.c"].classes) == [("point", 2)]
    assert [(i.module, i.line) for i in result["a.c"].imports] == [("stdio.h", 1)]
    assert names(result["a.rs"].functions) == [("A.new", 6)]
    assert names(result["a.rs"].classes) == [("A", 2)]  # the impl block is not a second class


def test_ruby_modules_and_other_import_roles() -> None:
    result = parse(
        _tag(name="M", path="a.rb", language="Ruby", kind="module", line=8, end=10, roles="def"),
        _tag(
            name="q",
            path="a.rb",
            language="Ruby",
            kind="method",
            line=9,
            end=9,
            scope="M",
            scopeKind="module",
        ),
        _tag(
            name="json",
            path="a.rb",
            language="Ruby",
            kind="library",
            line=1,
            roles="required",
            extras="reference",
        ),
        _tag(
            name="./lib.sh",
            path="a.sh",
            language="Sh",
            kind="script",
            line=1,
            roles="loaded",
            extras="reference",
        ),
        _tag(name="build", path="a.sh", language="Sh", kind="function", line=2),
    )

    assert names(result["a.rb"].classes) == [("M", 8)]
    assert names(result["a.rb"].functions) == [("M.q", 9)]
    assert [(i.module, i.line) for i in result["a.rb"].imports] == [("json", 1)]
    assert [(i.module, i.line) for i in result["a.sh"].imports] == [("./lib.sh", 1)]


def test_kotlin_top_level_fun_is_a_function_and_php_methods_are_qualified() -> None:
    result = parse(
        _tag(name="top", path="a.kt", language="Kotlin", kind="method", line=6),
        _tag(
            name="bar",
            path="a.kt",
            language="Kotlin",
            kind="method",
            line=2,
            scope="Foo",
            scopeKind="class",
        ),
        _tag(
            name="bar",
            path="a.php",
            language="PHP",
            kind="function",
            line=3,
            scope="Foo",
            scopeKind="class",
        ),
        _tag(name="Foo", path="a.php", language="PHP", kind="class", line=2),
    )

    assert names(result["a.kt"].functions) == [("Foo.bar", 2), ("top", 6)]
    assert names(result["a.php"].functions) == [("Foo.bar", 3)]


def test_malformed_lines_pseudo_tags_and_paths_outside_the_repo_are_ignored(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "ok.py").write_text("x = 1\n")
    lines = [
        "not json",
        "{",
        json.dumps({"_type": "ptag", "name": "JSON_OUTPUT_VERSION", "path": "ok.py"}),
        json.dumps({"_type": "tag", "name": "oops"}),  # no line, no path
        _tag(name="ok", path=str(root / "ok.py"), language="Python", kind="function", line=1),
        _tag(name="escaped", path="/etc/passwd", language="Python", kind="function", line=1),
        _tag(name="rel", path="./src/mod.py", language="Python", kind="function", line=3),
    ]
    result = parse_ctags_json_lines(lines, root=root)

    assert set(result) == {"ok.py", "src/mod.py"}
    assert names(result["ok.py"].functions) == [("ok", 1)]


def test_markup_and_data_files_do_not_contribute_symbols() -> None:
    result = parse(
        _tag(
            name="wrap", path="page.html", language="HTML", kind="class", line=10, roles="attribute"
        ),
        _tag(
            name=".sidebar", path="styles.css", language="CSS", kind="class", line=20, roles="def"
        ),
        _tag(
            name="dependencies",
            path="package.json",
            language="JSON",
            kind="object",
            line=5,
            roles="def",
        ),
        _tag(name="O", path="a.kt", language="Kotlin", kind="object", line=4, roles="def"),
        _tag(
            name="x",
            path="a.kt",
            language="Kotlin",
            kind="method",
            line=4,
            scope="O",
            scopeKind="object",
        ),
    )
    assert "page.html" not in result and "styles.css" not in result and "package.json" not in result
    assert names(result["a.kt"].classes) == [("O", 4)]
    assert names(result["a.kt"].functions) == [("O.x", 4)]


def test_classify_drops_tags_without_a_usable_name_or_line() -> None:
    assert classify_ctags_tag({"name": "", "kind": "function", "line": 1}) is None
    assert classify_ctags_tag({"name": "x", "kind": "function"}) is None


# --- Talking to the real binary (or a stub) -----------------------------------


def test_missing_or_unusable_ctags_is_a_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-ctags"
    assert probe_ctags(str(missing)) is None
    with pytest.raises(CtagsError, match="Universal Ctags is required"):
        require_ctags(str(missing))
    with pytest.raises(CtagsError, match="Universal Ctags is required"):
        extract_symbols_for_repository(tmp_path, ["a.py"], ctags_path=str(missing))


def test_a_failing_ctags_run_raises(tmp_path: Path) -> None:
    stub = tmp_path / "ctags"
    stub.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            if [ "$1" = "--version" ]; then
                echo "Universal Ctags 5.9.0, Copyright (C) 2015 Universal Ctags Team"
                exit 0
            fi
            if [ "$1" = "--list-features" ]; then
                echo "json"
                exit 0
            fi
            echo "boom: parser crashed" >&2
            exit 1
            """
        )
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "a.py").write_text("def x():\n    pass\n")

    with pytest.raises(CtagsError, match="failed while indexing"):
        extract_symbols_for_repository(tmp_path, ["a.py"], ctags_path=str(stub))


@pytest.mark.skipif(probe_ctags("ctags") is None, reason="Universal Ctags is not installed")
def test_real_ctags_extracts_symbols_from_a_fixture(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        textwrap.dedent(
            """\
            from core_lib.tokens import sign_token

            class Service:
                def run(self) -> None:
                    def helper() -> None:
                        pass

            def top() -> str:
                return sign_token("a", "b")
            """
        )
    )
    (tmp_path / "App.tsx").write_text(
        textwrap.dedent(
            """\
            import React from 'react';
            export async function load(url: string) {}
            export const handler = async () => {};
            export default class App {
              render() { return null; }
            }
            export interface Props { a: string }
            export function Chat() {
              const helper = () => 1;
              return null;
            }
            """
        )
    )

    result = extract_symbols_for_repository(tmp_path, ["pkg/mod.py", "App.tsx"], ctags_path="ctags")

    python = result["pkg/mod.py"]
    assert names(python.functions) == [("Service.run", 4), ("top", 8)]
    assert names(python.classes) == [("Service", 3)]
    assert [(i.module, i.line) for i in python.imports] == [("core_lib.tokens", 1)]
    # The helper nested in Service.run is not listed.
    assert all(s.name != "helper" and "helper" not in s.name for s in python.functions)

    tsx = result["App.tsx"]
    assert "load" in [s.name for s in tsx.functions]
    assert "Chat" in [s.name for s in tsx.functions]
    assert "handler" not in [s.name for s in tsx.functions]  # arrow assigned to const
    assert "App" in [s.name for s in tsx.classes]
    assert tsx.imports == ()  # Ctags 5.9 emits no JS/TS import tags
