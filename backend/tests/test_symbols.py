from __future__ import annotations

from repo_analyzer.ingestion.symbols import FileSymbols, extract_symbols


def names(symbols: tuple) -> list[tuple[str, int]]:  # type: ignore[type-arg]
    return [(s.name, s.line) for s in symbols]


def imports(result: FileSymbols) -> list[tuple[str, int]]:
    return [(i.module, i.line) for i in result.imports]


PYTHON = """\
import os, sys as system
from pathlib import Path
from . import sibling
from ..pkg.mod import thing


def top(a, b):
    def inner():
        pass
    import json
    return inner


async def fetch():
    ...


class Service(Base):
    def run(self):
        pass

    async def stop(self):
        pass

    class Config:
        def validate(self):
            pass
"""


def test_python_uses_ast_for_functions_classes_and_imports() -> None:
    result = extract_symbols("python", PYTHON)

    assert names(result.functions) == [
        ("top", 7),
        ("fetch", 14),
        ("Service.run", 19),
        ("Service.stop", 22),
        ("Service.Config.validate", 26),
    ]  # the helper nested inside ``top`` is not listed
    assert names(result.classes) == [("Service", 18), ("Service.Config", 25)]
    assert imports(result) == [
        ("os", 1),
        ("sys", 1),
        ("pathlib", 2),
        (".", 3),
        ("..pkg.mod", 4),
        ("json", 10),  # a lazy import inside a function still counts
    ]
    assert [f.end_line for f in result.functions[:2]] == [11, 15]
    assert result.to_dict()["functions"][1] == {"name": "fetch", "line": 14, "end_line": 15}


def test_python_with_a_syntax_error_falls_back_to_regexes() -> None:
    result = extract_symbols("python", "import os\n\ndef ok():\n    pass\n\nprint 'python 2'\n")

    assert imports(result) == [("os", 1)]
    assert names(result.functions) == [("ok", 3)]


def test_javascript_and_typescript() -> None:
    source = """\
import React, { useState } from 'react';
import './styles.css';
import {
  a,
  b,
} from "./lib/ab";
export { x } from './x';
const fs = require('fs');

// function commented() {}
export async function load(url: string) {}
function* gen() {}
export const handler = async (req, res) => {};
const double = n => n * 2;
const legacy = function () {};
const notAFunction = 42;

export default class App extends Component {}
export interface Props { a: string }
export enum Mode { A, B }
"""
    for language in ("javascript", "typescript"):
        result = extract_symbols(language, source)

        assert imports(result) == [
            ("react", 1),
            ("./styles.css", 2),
            ("./lib/ab", 6),
            ("./x", 7),
            ("fs", 8),
        ]
        assert names(result.functions) == [
            ("load", 11),
            ("gen", 12),
            ("handler", 13),
            ("double", 14),
            ("legacy", 15),
        ]
        assert names(result.classes) == [("App", 18), ("Props", 19), ("Mode", 20)]


def test_go() -> None:
    source = """\
package main

import "fmt"
import alias "net/http"

import (
\t"os"
\tlog "github.com/sirupsen/logrus" // logging
)

type Server struct {
\taddr string
}

type Handler interface {
\tServe()
}

type ID int

func main() {}

func (s *Server) Start(port int) error { return nil }

func (Server) Name() string { return "" }

func Map[T any](xs []T) []T { return xs }
"""
    result = extract_symbols("go", source)

    assert imports(result) == [
        ("fmt", 3),
        ("net/http", 4),
        ("os", 7),
        ("github.com/sirupsen/logrus", 8),
    ]
    assert names(result.classes) == [("Server", 11), ("Handler", 15)]
    assert names(result.functions) == [
        ("main", 21),
        ("Server.Start", 23),
        ("Server.Name", 25),
        ("Map", 27),
    ]


def test_other_languages_use_simple_heuristics() -> None:
    rust = extract_symbols(
        "rust",
        "use std::collections::HashMap;\npub struct A;\n"
        "pub(crate) async fn go() {}\nfn main() {}\n",
    )
    assert imports(rust) == [("std::collections::HashMap", 1)]
    assert names(rust.classes) == [("A", 2)]
    assert names(rust.functions) == [("go", 3), ("main", 4)]

    java = extract_symbols(
        "java",
        "import java.util.List;\npublic class Foo {\n    public static void main(String[] a) {\n"
        "    }\n}\n",
    )
    assert imports(java) == [("java.util.List", 1)]
    assert names(java.classes) == [("Foo", 2)]
    assert names(java.functions) == [("main", 3)]

    ruby = extract_symbols(
        "ruby", "require 'json'\n# def commented\nclass Foo\n  def bar\n  end\nend\n"
    )
    assert imports(ruby) == [("json", 1)]
    assert names(ruby.classes) == [("Foo", 3)]
    assert names(ruby.functions) == [("bar", 4)]

    shell = extract_symbols(
        "shell", "source ./lib.sh\n# build() {\nbuild() {\n}\nfunction deploy {\n}\n"
    )
    assert imports(shell) == [("./lib.sh", 1)]
    assert names(shell.functions) == [("build", 3), ("deploy", 5)]

    c = extract_symbols("c", '#include <stdio.h>\n#include "mine.h"\nstruct point {\n};\n')
    assert imports(c) == [("stdio.h", 1), ("mine.h", 2)]
    assert names(c.classes) == [("point", 3)]


def test_unsupported_languages_and_minified_lines_yield_nothing() -> None:
    assert extract_symbols("markdown", "# def not_code():\n").is_empty()
    assert extract_symbols("json", '{"import": "x"}').is_empty()
    minified = "function a(){}" + "x" * 600
    assert extract_symbols("javascript", minified).is_empty()
