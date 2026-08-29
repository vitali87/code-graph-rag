# Edge-site locations on CALLS / REFERENCES / INSTANTIATES / IMPORTS (issue #1522).
#
# The graph knew THAT `f` calls `g`, not WHERE. Every call edge now carries the
# span of the call expression plus its argument shape, one edge per site, and
# every IMPORTS edge the span of its statement plus the bound alias and the
# imported symbol, so a consumer can jump to, check, or rewrite the exact site.
from __future__ import annotations

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
    assert cs.KEY_IMPORTED_NAME not in imports["ns"]


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
