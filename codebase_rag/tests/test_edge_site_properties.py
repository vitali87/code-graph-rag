# Edge-site locations on CALLS / REFERENCES / INSTANTIATES / IMPORTS (issue #1522).
#
# The graph knew THAT `f` calls `g`, not WHERE. Every call edge now carries the
# span of the call expression plus its argument shape, one edge per site, and
# every IMPORTS edge the span of its statement plus the bound alias and the
# imported symbol, so a consumer can jump to, check, or rewrite the exact site.
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import codec.schema_pb2 as pb
from codebase_rag import constants as cs
from codebase_rag.cypher_queries import build_merge_relationship_query
from codebase_rag.services.graph_diff import _rel_map
from codebase_rag.services.protobuf_service import ProtobufFileIngestor
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.types_defs import PropertyDict


def _span(text: str, needle: str, occurrence: int = 0) -> tuple[int, int, int, int]:
    """(line, col, end_line, end_col) of the n-th `needle` in `text`."""
    idx = -1
    for _ in range(occurrence + 1):
        idx = text.index(needle, idx + 1)
    line = text.count("\n", 0, idx) + 1
    col = idx - (text.rfind("\n", 0, idx) + 1)
    end = idx + len(needle)
    end_line = text.count("\n", 0, end) + 1
    end_col = end - (text.rfind("\n", 0, end) + 1)
    return line, col, end_line, end_col


def _edges(
    mock: MagicMock, rel_type: str
) -> list[tuple[str, str, PropertyDict | None]]:
    out = []
    for c in mock.ensure_relationship_batch.call_args_list:
        if c.args[1] != rel_type:
            continue
        props = c.kwargs.get("properties")
        if props is None and len(c.args) > 3:
            props = c.args[3]
        out.append((str(c.args[0][2]), str(c.args[2][2]), props))
    return out


def _site(props: PropertyDict | None) -> tuple[Any, ...]:
    assert props is not None
    return (
        props[cs.KEY_LINE],
        props[cs.KEY_COL],
        props[cs.KEY_END_LINE],
        props[cs.KEY_END_COL],
    )


def _calls_between(
    mock: MagicMock, caller_suffix: str, callee_suffix: str
) -> list[PropertyDict]:
    out = []
    for src, dst, props in _edges(mock, cs.RelationshipType.CALLS):
        if src.endswith(caller_suffix) and dst.endswith(callee_suffix):
            assert props is not None, (src, dst)
            out.append(props)
    return out


def _imports_from(
    mock: MagicMock, module_suffix: str
) -> list[tuple[str, PropertyDict]]:
    return [
        (dst, props)
        for src, dst, props in _edges(mock, cs.RelationshipType.IMPORTS)
        if src.endswith(module_suffix) and props is not None
    ]


# --- Python --------------------------------------------------------------------

PY_UTIL = "def f():\n    return 1\n"
PY_MAIN = (
    "import os\n"
    "from json import loads as jl\n"
    "from .util import f\n"
    "\n"
    "\n"
    "def helper(a, b=1):\n"
    "    return a\n"
    "\n"
    "\n"
    "def caller():\n"
    "    helper(1)\n"
    "    return helper(2, b=3) + f()\n"
)


def _write_python_repo(root: Path) -> None:
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "util.py").write_text(PY_UTIL, encoding="utf-8")
    (pkg / "main.py").write_text(PY_MAIN, encoding="utf-8")


def test_python_call_edges_carry_one_site_per_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_python_repo(temp_repo)
    create_and_run_updater(temp_repo, mock_ingestor)

    sites = sorted(
        (_site(p), p[cs.KEY_ARG_COUNT], p[cs.KEY_KWARG_NAMES])
        for p in _calls_between(mock_ingestor, "pkg.main.caller", "pkg.main.helper")
    )
    assert sites == [
        (_span(PY_MAIN, "helper(1)"), 1, []),
        (_span(PY_MAIN, "helper(2, b=3)"), 2, ["b"]),
    ]
    (f_props,) = _calls_between(mock_ingestor, "pkg.main.caller", "pkg.util.f")
    assert _site(f_props) == _span(PY_MAIN, "f()")
    assert f_props[cs.KEY_ARG_COUNT] == 0


def test_python_import_edges_carry_statement_alias_and_symbol(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _write_python_repo(temp_repo)
    create_and_run_updater(temp_repo, mock_ingestor)

    by_alias = {
        props[cs.KEY_ALIAS]: (dst, props)
        for dst, props in _imports_from(mock_ingestor, "pkg.main")
    }
    assert set(by_alias) == {"os", "jl", "f"}

    dst, props = by_alias["os"]
    assert _site(props) == _span(PY_MAIN, "import os")
    assert cs.KEY_IMPORTED_NAME not in props

    dst, props = by_alias["jl"]
    assert _site(props) == _span(PY_MAIN, "from json import loads as jl")
    assert props[cs.KEY_IMPORTED_NAME] == "loads"

    dst, props = by_alias["f"]
    assert dst.endswith("pkg.util")
    assert _site(props) == _span(PY_MAIN, "from .util import f")
    assert props[cs.KEY_IMPORTED_NAME] == "f"


# --- TypeScript ----------------------------------------------------------------

TS_A = "export function helper(x: number): number {\n  return x;\n}\n"
TS_B = (
    'import { helper as h } from "./a";\n'
    'import * as ns from "./a";\n'
    "\n"
    "export function caller(): number {\n"
    "  return h(1) + h(2, 3) + ns.helper(4);\n"
    "}\n"
)


def test_typescript_call_and_import_sites(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    src = temp_repo / "src"
    src.mkdir()
    (src / "a.ts").write_text(TS_A, encoding="utf-8")
    (src / "b.ts").write_text(TS_B, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    sites = sorted(
        (_site(p), p[cs.KEY_ARG_COUNT])
        for p in _calls_between(mock_ingestor, "src.b.caller", "src.a.helper")
    )
    assert sites == [
        (_span(TS_B, "h(1)"), 1),
        (_span(TS_B, "h(2, 3)"), 2),
        (_span(TS_B, "ns.helper(4)"), 1),
    ]

    imports = {
        props[cs.KEY_ALIAS]: props
        for _dst, props in _imports_from(mock_ingestor, "src.b")
    }
    assert _site(imports["h"]) == _span(TS_B, 'import { helper as h } from "./a";')
    assert imports["h"][cs.KEY_IMPORTED_NAME] == "helper"
    assert _site(imports["ns"]) == _span(TS_B, 'import * as ns from "./a";')
    assert imports["ns"][cs.KEY_IMPORTED_NAME] == cs.IMPORTED_NAME_WILDCARD


# --- Go ------------------------------------------------------------------------

GO_M = (
    "package p\n"
    "\n"
    'import (\n\t"fmt"\n\tstr "strings"\n)\n'
    "\n"
    "func free(a int) int { return a }\n"
    "\n"
    "func caller() int {\n"
    '\tfmt.Println(str.ToUpper("x"))\n'
    "\treturn free(1) + free(2)\n"
    "}\n"
)


def test_go_call_and_import_sites(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    pkg = temp_repo / "p"
    pkg.mkdir()
    (pkg / "m.go").write_text(GO_M, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    sites = sorted(
        _site(p) for p in _calls_between(mock_ingestor, "p.m.caller", "p.m.free")
    )
    assert sites == [_span(GO_M, "free(1)"), _span(GO_M, "free(2)")]

    imports = {
        props[cs.KEY_ALIAS]: props
        for _dst, props in _imports_from(mock_ingestor, "p.m")
    }
    # A grouped import block records each spec line as its own site.
    assert _site(imports["fmt"]) == _span(GO_M, '"fmt"')
    assert _site(imports["str"]) == _span(GO_M, 'str "strings"')


# --- Java ----------------------------------------------------------------------

JAVA_A = (
    "package com.x;\n"
    "\n"
    "import java.util.List;\n"
    "\n"
    "public class A {\n"
    "    static int helper(int a) { return a; }\n"
    "    static int caller() { return helper(1) + helper(2); }\n"
    "}\n"
)


def test_java_call_and_import_sites(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    pkg = temp_repo / "src" / "main" / "java" / "com" / "x"
    pkg.mkdir(parents=True)
    (pkg / "A.java").write_text(JAVA_A, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    sites = sorted(
        _site(p) for p in _calls_between(mock_ingestor, ".A.caller()", ".A.helper(int)")
    )
    assert sites == [_span(JAVA_A, "helper(1)"), _span(JAVA_A, "helper(2)")]

    ((_dst, props),) = _imports_from(mock_ingestor, "com.x.A")
    assert _site(props) == _span(JAVA_A, "import java.util.List;")
    assert props[cs.KEY_ALIAS] == "List"
    assert props[cs.KEY_IMPORTED_NAME] == "List"


# --- Rust ----------------------------------------------------------------------

RUST_MAIN = (
    "use std::collections::HashMap as Map;\n"
    "\n"
    "fn helper(a: i32) -> i32 {\n"
    "    a\n"
    "}\n"
    "\n"
    "fn main() {\n"
    "    let _m: Map<i32, i32> = Map::new();\n"
    "    helper(1);\n"
    "    helper(2);\n"
    "}\n"
)


def test_rust_call_and_import_sites(temp_repo: Path, mock_ingestor: MagicMock) -> None:
    src = temp_repo / "src"
    src.mkdir()
    (temp_repo / "Cargo.toml").write_text(
        '[package]\nname = "app"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (src / "main.rs").write_text(RUST_MAIN, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)

    sites = sorted(
        _site(p) for p in _calls_between(mock_ingestor, "main.main", "main.helper")
    )
    assert sites == [_span(RUST_MAIN, "helper(1)"), _span(RUST_MAIN, "helper(2)")]

    imports = [props for _dst, props in _imports_from(mock_ingestor, "src.main")]
    (props,) = imports
    assert _site(props) == _span(RUST_MAIN, "use std::collections::HashMap as Map;")
    assert props[cs.KEY_ALIAS] == "Map"
    assert props[cs.KEY_IMPORTED_NAME] == "HashMap"


# --- Sink semantics ------------------------------------------------------------


def test_site_props_join_the_merge_key() -> None:
    # Two calls from one caller to one callee must stay two edges at write
    # time; the merge pattern keys on the site the same way FLOWS_TO keys on
    # `via`/`kind` (issue #722).
    for rel in (
        cs.RelationshipType.CALLS,
        cs.RelationshipType.REFERENCES,
        cs.RelationshipType.INSTANTIATES,
    ):
        assert cs.MERGE_KEY_PROPS_BY_REL[rel.value] == (cs.KEY_LINE, cs.KEY_COL)
    assert cs.MERGE_KEY_PROPS_BY_REL[cs.RelationshipType.IMPORTS.value] == (
        cs.KEY_LINE,
        cs.KEY_COL,
        cs.KEY_ALIAS,
    )
    query = build_merge_relationship_query(
        "Function",
        "qualified_name",
        "CALLS",
        "Function",
        "qualified_name",
        has_props=True,
        merge_key_props=cs.MERGE_KEY_PROPS_BY_REL["CALLS"],
    )
    assert (
        "MERGE (a)-[r:CALLS {line: row.props.line, col: row.props.col}]->(b)" in query
    )


def _call_row(
    line: int, col: int
) -> tuple[tuple[str, str, str], str, tuple[str, str, str], PropertyDict]:
    return (
        ("Function", "qualified_name", "p.m.caller"),
        "CALLS",
        ("Function", "qualified_name", "p.m.helper"),
        {
            cs.KEY_LINE: line,
            cs.KEY_COL: col,
            cs.KEY_END_LINE: line,
            cs.KEY_END_COL: col + 9,
            cs.KEY_ARG_COUNT: 1,
            cs.KEY_KWARG_NAMES: [],
        },
    )


def test_protobuf_export_keeps_one_relationship_per_site(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    ingestor = ProtobufFileIngestor(str(out), split_index=False)
    for spec in ("p.m.caller", "p.m.helper"):
        ingestor.ensure_node_batch(
            "Function", {"qualified_name": spec, "name": spec.rsplit(".", 1)[-1]}
        )
    ingestor.ensure_relationship_batch(*_call_row(11, 4))
    ingestor.ensure_relationship_batch(*_call_row(12, 11))
    # A site-less row (a libclang macro use, a trace write-back) still
    # collapses on its endpoints rather than minting a third edge per flush.
    ingestor.ensure_relationship_batch(*_call_row(11, 4)[:3])
    ingestor.ensure_relationship_batch(*_call_row(11, 4)[:3])
    ingestor.flush_all()

    index = pb.GraphCodeIndex()
    index.ParseFromString((out / "index.bin").read_bytes())
    calls = [r for r in index.relationships if r.type == pb.Relationship.CALLS]
    assert len(calls) == 3
    sited = sorted(
        (int(p["line"]), int(p["col"]), int(p["arg_count"]))
        for p in (dict(r.properties) for r in calls)
        if "line" in p
    )
    assert sited == [(11, 4, 1), (12, 11, 1)]
    (bare,) = [r for r in calls if "line" not in r.properties]
    assert bare.source_id == "p.m.caller"

    # graph diff stays structural: per-site edges fold onto their triple and
    # the site props drop out, so a line shift is not a relationship change.
    rels = _rel_map([index])
    assert rels == {("p.m.caller", "CALLS", "p.m.helper"): {}}


@pytest.mark.parametrize(
    "rel_type",
    [cs.RelationshipType.CALLS, cs.RelationshipType.REFERENCES],
)
def test_reference_site_without_arguments_carries_span_only(
    temp_repo: Path, mock_ingestor: MagicMock, rel_type: str
) -> None:
    # `{"k": helper}` references helper by value: the site is the bare name,
    # with no argument list and therefore no arg_count.
    src = 'def helper():\n    return 1\n\n\ndef build():\n    return {"k": helper}\n'
    (temp_repo / "m.py").write_text(src, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)
    matches = [
        props
        for s, d, props in _edges(mock_ingestor, rel_type)
        if s.endswith("m.build") and d.endswith("m.helper") and props is not None
    ]
    if not matches:
        pytest.skip(f"dict-value reference is not emitted as {rel_type}")
    (props,) = matches
    # The site is the referencing expression itself, not the enclosing
    # `return` statement or dict literal.
    assert _site(props) == _span(src, "helper", occurrence=1)
    assert cs.KEY_ARG_COUNT not in props


# --- Review findings on PR #1537 ------------------------------------------------


def _import_props(mock: MagicMock, module_suffix: str) -> dict[str, PropertyDict]:
    """alias -> site props for every sited IMPORTS edge out of one module."""
    return {
        str(props[cs.KEY_ALIAS]): props
        for _dst, props in _imports_from(mock, module_suffix)
        if cs.KEY_ALIAS in props
    }


CSHARP_SRC = "using System.Text;\nusing IO = System.IO;\n\nclass A { }\n"
CPP_SRC = '#include <vector>\n#include "util.h"\n\nint main() { return 0; }\n'
PHP_SRC = "<?php\nuse App\\Models\\User as U;\nuse App\\Helpers\\Fmt;\n\nclass A {}\n"
DART_SRC = (
    "import 'package:flutter/material.dart';\nimport 'util.dart';\n\nclass A {}\n"
)
LUA_SRC = "local Person = require('person')\nlocal json = require('json')\n"
SCALA_SRC = (
    "package com.example\n"
    "import scala.collection.mutable.{Map => MMap}\n"
    "import scala.util.Try\n"
    "import scala.io._\n\n"
    "object Solo { def run(): Int = 1 }\n"
)


@pytest.mark.parametrize(
    ("grammar", "filename", "src", "extra_files", "expected"),
    [
        (
            "c_sharp",
            "A.cs",
            CSHARP_SRC,
            {},
            {
                "Text": ("using System.Text;", "Text"),
                "IO": ("using IO = System.IO;", "IO"),
            },
        ),
        (
            "cpp",
            "main.cpp",
            CPP_SRC,
            {"util.h": "int util();\n"},
            {
                # tree-sitter's preproc_include spans through its newline.
                "vector": ("#include <vector>\n", "vector"),
                "util": ('#include "util.h"\n', "util.h"),
            },
        ),
        (
            "php",
            "a.php",
            PHP_SRC,
            {},
            {
                "U": ("use App\\Models\\User as U;", "User"),
                "Fmt": ("use App\\Helpers\\Fmt;", "Fmt"),
            },
        ),
        (
            "dart",
            "a.dart",
            DART_SRC,
            {"util.dart": "int util() => 1;\n"},
            {
                "material": (
                    "import 'package:flutter/material.dart';",
                    "package:flutter/material.dart",
                ),
                "util": ("import 'util.dart';", "util.dart"),
            },
        ),
        (
            "lua",
            "main.lua",
            LUA_SRC,
            {"person.lua": "local Person = {}\nreturn Person\n"},
            {
                "Person": ("require('person')", "person"),
                "json": ("require('json')", "json"),
            },
        ),
        (
            "scala",
            "a.scala",
            SCALA_SRC,
            {},
            {
                "MMap": ("import scala.collection.mutable.{Map => MMap}", "Map"),
                "Try": ("import scala.util.Try", "Try"),
            },
        ),
    ],
)
def test_every_import_handler_records_the_site(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    grammar: str,
    filename: str,
    src: str,
    extra_files: dict[str, str],
    expected: dict[str, tuple[str, str]],
) -> None:
    """C#, C++, PHP, Dart, Lua and Scala imports carry a span, alias and name.

    The first cut of #1522 wired only the five languages the acceptance list
    named; the rest still emitted property-less IMPORTS edges, so two
    bindings of one provider collapsed onto a single edge (Greptile P1).
    """
    for name, body in extra_files.items():
        (temp_repo / name).write_text(body, encoding="utf-8")
    (temp_repo / filename).write_text(src, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing=grammar)

    sited = _import_props(mock_ingestor, Path(filename).stem)
    for alias, (needle, imported_name) in expected.items():
        assert alias in sited, (alias, sorted(sited))
        assert _site(sited[alias]) == _span(src, needle), alias
        assert sited[alias][cs.KEY_IMPORTED_NAME] == imported_name, alias


def test_scala_wildcard_import_records_span_without_alias(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    (temp_repo / "a.scala").write_text(SCALA_SRC, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="scala")
    wildcard = [
        props
        for dst, props in _imports_from(mock_ingestor, ".a")
        if dst.rpartition(".")[2] == "io" and "scala" in dst.split(".")
    ]
    (props,) = wildcard
    assert _site(props) == _span(SCALA_SRC, "import scala.io._")
    assert cs.KEY_ALIAS not in props


def test_rust_reparse_retracts_sub_scope_import_sites(temp_repo: Path) -> None:
    """A re-parsed Rust file drops the sub-scope site entries it wrote before.

    Sub-scope uses record their site under the fn / inline-mod scope qn, not
    the file's, so the per-module reset alone left the removed binding's span
    standing beside its replacement (Greptile P1 on #1537).
    """
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    processor = ImportProcessor(repo_path=temp_repo, project_name="p")
    module_qn = "p.src.worker"

    def parse(source: str) -> None:
        tree = parsers[cs.SupportedLanguage.RUST].parse(source.encode())
        processor.parse_imports(
            tree.root_node, module_qn, cs.SupportedLanguage.RUST, queries
        )

    parse("mod nested {\n    use std::fmt::Display as Inline;\n}\n")
    scope = f"{module_qn}.nested"
    assert set(processor._import_sites[scope]) == {"Inline"}

    parse("mod nested {\n    use std::fmt::Debug as Replacement;\n}\n")
    assert set(processor._import_sites[scope]) == {"Replacement"}

    parse("fn main() {}\n")
    assert scope not in processor._import_sites


def test_call_site_cache_is_not_keyed_by_recycled_node_ids() -> None:
    """A fresh tree whose node id happens to match must not reuse a cached site.

    tree-sitter recycles node ids across trees, so a cache keyed by bare
    `Node.id` could hand a later call the span and argument shape of an
    earlier, unrelated one (Greptile P1 on #1537). Keying by the node object
    (and holding it) makes a hit impossible for any node but that one.
    """
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.call_processor import CallProcessor

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    parser = parsers[cs.SupportedLanguage.PYTHON]

    def first_call(source: str) -> Any:
        tree = parser.parse(source.encode())
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == "call":
                return node
            stack.extend(node.children)
        raise AssertionError("no call node")

    ingestor = MagicMock()
    cp = CallProcessor.__new__(CallProcessor)
    cp.ingestor = ingestor
    cp._site_node = None
    cp._site_cache = None
    cp._resolution = cs.EdgeResolution.EXACT

    src_a = "f(1)\n"
    src_b = "g(1, 2, k=3)\n"
    call_a = first_call(src_a)
    call_b = first_call(src_b)
    cp._site_node = call_a
    cp._emit_rel(
        ("Function", "qualified_name", "x"),
        "CALLS",
        ("Function", "qualified_name", "f"),
    )
    cp._site_node = call_b
    cp._emit_rel(
        ("Function", "qualified_name", "x"),
        "CALLS",
        ("Function", "qualified_name", "g"),
    )

    (first, second) = ingestor.ensure_relationship_batch.call_args_list
    assert first.kwargs["properties"][cs.KEY_ARG_COUNT] == 1
    assert second.kwargs["properties"][cs.KEY_ARG_COUNT] == 3
    assert second.kwargs["properties"][cs.KEY_KWARG_NAMES] == ["k"]
    assert _site(second.kwargs["properties"]) == _span(src_b, "g(1, 2, k=3)")
    # An id collision cannot be forced from a test, so the behavioural check
    # above passes on the old code too; the key must be the node itself.
    assert cp._site_cache is not None
    assert cp._site_cache[0] is call_b


CPP_CONSTRUCTIONS = (
    "class Point {\n"
    " public:\n"
    "  Point(int x, int y) : x_(x), y_(y) {}\n"
    "  int x_;\n"
    "  int y_;\n"
    "};\n"
    "\n"
    "class Line : public Point {\n"
    " public:\n"
    "  Line() : Point(0, 0) {}\n"
    "};\n"
    "\n"
    "Point make() { return {1, 2}; }\n"
    "\n"
    "int main() {\n"
    "  Point origin(3, 4);\n"
    "  return origin.x_;\n"
    "}\n"
)


def test_cpp_construction_paths_carry_their_site(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """Declaration, member-initializer and braced-return constructions have
    no call node; their edges still carry the statement that constructs.

    The per-call loop pinned the site for explicit `Point(3, 4)` calls only;
    these three C++ paths emitted edges with no site at all (CodeRabbit on
    #1537).
    """
    (temp_repo / "main.cpp").write_text(CPP_CONSTRUCTIONS, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")

    def sites(caller_suffix: str) -> set[tuple[Any, ...]]:
        found = set()
        for rel in (cs.RelationshipType.CALLS, cs.RelationshipType.INSTANTIATES):
            for src, dst, props in _edges(mock_ingestor, rel):
                if (
                    re.search(rf"{re.escape(caller_suffix)}(\(|$)", src)
                    and "Point" in dst
                ):
                    assert props is not None, (src, dst)
                    found.add(_site(props))
        return found

    assert sites(".main") == {_span(CPP_CONSTRUCTIONS, "Point origin(3, 4);")}
    assert sites(".Line.Line") == {_span(CPP_CONSTRUCTIONS, "Point(0, 0)")}
    assert sites(".make") == {_span(CPP_CONSTRUCTIONS, "return {1, 2};")}


def test_rust_reparse_keeps_a_sibling_writers_sub_scope_site(temp_repo: Path) -> None:
    """Two files can write the same sub-scope key and name (an inline mod in
    src/a.rs and src/a/b/mod.rs both resolve to `p.src.a.b`); re-parsing one
    must not drop the entry the OTHER wrote last (Greptile P1 on #1537)."""
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    processor = ImportProcessor(repo_path=temp_repo, project_name="p")

    def parse(module_qn: str, source: str) -> None:
        tree = parsers[cs.SupportedLanguage.RUST].parse(source.encode())
        processor.parse_imports(
            tree.root_node, module_qn, cs.SupportedLanguage.RUST, queries
        )

    scope = "p.src.a.b"
    # src/a.rs declares an inline `mod b` with a use; its scope key is p.src.a.b.
    parse("p.src.a", "mod b {\n    use std::fmt::Display as Shared;\n}\n")
    assert "Shared" in processor._import_sites[scope]
    # src/a/b/mod.rs is the child file whose OWN scope is p.src.a.b (a file
    # module writes its file-level sites directly under its qn).
    parse(scope, "use std::fmt::Debug as Shared;\n")
    child_site = processor._import_sites[scope]["Shared"]
    assert child_site[cs.KEY_IMPORTED_NAME] == "Debug"

    # Re-parsing src/a.rs without the use must leave the child's entry alone.
    parse("p.src.a", "fn main() {}\n")
    assert processor._import_sites[scope]["Shared"] is child_site


def test_rust_file_module_reparse_keeps_the_inline_siblings_entries(
    temp_repo: Path,
) -> None:
    """The mirror case: re-parsing the FILE-backed module whose key an inline
    mod of another file also writes must keep that inline mod's entries,
    including names the file never bound (CodeRabbit on #1537)."""
    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.parsers.import_processor import ImportProcessor

    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("rust parser not available")
    processor = ImportProcessor(repo_path=temp_repo, project_name="p")

    def parse(module_qn: str, source: str) -> None:
        tree = parsers[cs.SupportedLanguage.RUST].parse(source.encode())
        processor.parse_imports(
            tree.root_node, module_qn, cs.SupportedLanguage.RUST, queries
        )

    scope = "p.src.a.b"
    parse(scope, "use std::fmt::Debug as Own;\n")
    parse("p.src.a", "mod b {\n    use std::fmt::Display as Inline;\n}\n")
    inline_site = processor._import_sites[scope]["Inline"]

    parse(scope, "use std::fmt::Write as Own2;\n")
    assert processor._import_sites[scope]["Inline"] is inline_site
    assert "Own" not in processor._import_sites[scope]
    assert "Own2" in processor._import_sites[scope]


def test_incremental_update_restores_inbound_edges_with_their_sites(
    temp_repo: Path,
) -> None:
    """An untouched caller's edge into a re-indexed file keeps its site.

    Per-site edges are keyed by their site, so restoring the captured edge
    bare would land a second, site-less edge beside the original.
    """
    from codebase_rag.graph_updater import GraphUpdater
    from codebase_rag.parser_loader import load_parsers

    sink = MagicMock()
    site = {cs.KEY_LINE: 5, cs.KEY_COL: 4, cs.KEY_END_LINE: 5, cs.KEY_END_COL: 9}
    parsers, queries = load_parsers()
    updater = GraphUpdater(sink, temp_repo, parsers, queries)
    updater.function_registry["proj.pkg.util.helper"] = cs.NodeLabel.FUNCTION.value
    updater._restore_inbound_edges(
        [
            {
                cs.KEY_CALLER_LABEL: cs.NodeLabel.FUNCTION.value,
                cs.KEY_CALLER_QN: "proj.main.main",
                cs.KEY_REL: cs.RelationshipType.CALLS.value,
                cs.KEY_TARGET_LABEL: cs.NodeLabel.FUNCTION.value,
                cs.KEY_TARGET_QN: "proj.pkg.util.helper",
                cs.KEY_PROPS: dict(site),
            },
            {
                cs.KEY_CALLER_LABEL: cs.NodeLabel.MODULE.value,
                cs.KEY_CALLER_QN: "proj.main",
                cs.KEY_REL: cs.RelationshipType.IMPORTS.value,
                cs.KEY_TARGET_LABEL: cs.NodeLabel.MODULE.value,
                cs.KEY_TARGET_QN: "proj.pkg.util",
                cs.KEY_PROPS: {},
            },
        ]
    )
    calls = sink.ensure_relationship_batch.call_args_list
    assert len(calls) == 2
    assert calls[0].kwargs[cs.KEY_PROPERTIES] == site
    assert calls[1].kwargs[cs.KEY_PROPERTIES] is None
    assert "properties(r) AS props" in cs.CYPHER_INBOUND_EDGES
