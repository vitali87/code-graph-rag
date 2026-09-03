# Deterministic graph queries (issue #1523): fixed Cypher plus client-side
# walks, project-scoped, sorted, no LLM in the path. The fake graph below is
# the fixture repo in graph form; every tool is asserted to be a pure
# function of it (same graph, same JSON) and `callers` to return one row per
# call site from the edge locations of issue #1522.
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.graph_cli import cli as graph_cli
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.types_defs import PropertyDict, ResultRow

P = "proj"


def _node(
    label: str, qn: str, path: str, start: int, end: int, **extra: Any
) -> ResultRow:
    row: ResultRow = {
        cs.KEY_LABEL: label,
        cs.KEY_QUALIFIED_NAME: qn,
        cs.KEY_NAME: qn.rsplit(".", 1)[-1],
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
        cs.KEY_DECORATORS: [],
        cs.KEY_DOCSTRING: extra.get("docstring"),
    }
    return row


NODES: list[ResultRow] = [
    _node("Module", f"{P}.app", "app.py", 1, 20),
    _node("Module", f"{P}.util", "util.py", 1, 10),
    _node("Module", f"{P}.tests.test_app", "tests/test_app.py", 1, 30),
    _node("Function", f"{P}.util.helper", "util.py", 1, 4, docstring="Help."),
    _node("Function", f"{P}.app.run", "app.py", 3, 8),
    _node("Function", f"{P}.app.main", "app.py", 10, 14),
    _node("Class", f"{P}.app.Base", "app.py", 15, 17),
    _node("Class", f"{P}.app.Child", "app.py", 18, 20),
    _node("Method", f"{P}.app.Base.go", "app.py", 16, 17),
    _node("Method", f"{P}.app.Child.go", "app.py", 19, 20),
    _node("Function", f"{P}.tests.test_app.test_run", "tests/test_app.py", 1, 5),
    _node("Function", f"{P}.tests.test_app.test_main", "tests/test_app.py", 7, 12),
    _node("Function", f"{P}.tests.test_app.setup", "tests/test_app.py", 14, 16),
]

# (from_qn, to_qn, line, col, end_line, end_col, arg_count, kwarg_names)
CALLS: list[
    tuple[
        str,
        str,
        int | None,
        int | None,
        int | None,
        int | None,
        int | None,
        list[str] | None,
    ]
] = [
    (f"{P}.app.run", f"{P}.util.helper", 4, 11, 4, 20, 1, []),
    (f"{P}.app.run", f"{P}.util.helper", 6, 11, 6, 26, 2, ["b"]),
    (f"{P}.app.main", f"{P}.app.run", 12, 4, 12, 9, 0, []),
    (f"{P}.tests.test_app.test_run", f"{P}.app.run", 3, 4, 3, 9, 0, []),
    (f"{P}.tests.test_app.test_main", f"{P}.app.main", 9, 4, 9, 10, 0, []),
    # An edge written without a site (a frontend fact).
    (
        f"{P}.tests.test_app.setup",
        f"{P}.util.helper",
        None,
        None,
        None,
        None,
        None,
        None,
    ),
]
INHERITS = [(f"{P}.app.Child", f"{P}.app.Base", "INHERITS")]
OVERRIDES = [(f"{P}.app.Child.go", f"{P}.app.Base.go")]
IMPORTS = [
    (f"{P}.app", f"{P}.util", 1, 0, 1, 27, "helper", "helper"),
    (f"{P}.tests.test_app", f"{P}.util", 2, 0, 2, 22, "util", "util"),
]


def _by_qn(qn: str) -> ResultRow:
    return next(n for n in NODES if n[cs.KEY_QUALIFIED_NAME] == qn)


# Where the fixture project was indexed from; a test sets it to its own root
# to be allowed to read source, any other value leaves the span source-less.
PROJECT_ROOT = "/elsewhere/foreign-checkout"


def fake_fetch_all(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
    """The fixture graph answering exactly the fixed queries the tools issue."""
    p = params or {}
    prefix = str(p.get(cs.KEY_PROJECT_PREFIX, ""))
    assert prefix == f"{P}.", "every query is project-scoped"
    qn = str(p.get(cs.KEY_QN, ""))
    out: list[ResultRow] = []
    if query == cq.CYPHER_PROJECT_ROOT_PATH:
        return [{cs.KEY_ROOT_PATH: PROJECT_ROOT}]
    if query == cq.CYPHER_GRAPH_RESOLVE_NAME:
        for n in NODES:
            q = str(n[cs.KEY_QUALIFIED_NAME])
            if (
                q == qn
                or q.endswith(str(p[cs.KEY_SUFFIX]))
                or n[cs.KEY_NAME] == p[cs.KEY_NAME]
            ):
                out.append(n)
    elif query == cq.CYPHER_GRAPH_RESOLVE_LOCATION:
        line = int(p[cs.KEY_LINE])  # type: ignore[arg-type]
        for n in NODES:
            if n[cs.KEY_PATH] == p[cs.KEY_PATH] and int(
                n[cs.KEY_START_LINE]
            ) <= line <= int(n[cs.KEY_END_LINE]):  # type: ignore[arg-type]
                out.append(n)
    elif query == cq.CYPHER_GRAPH_DEFINITION:
        out = [n for n in NODES if n[cs.KEY_QUALIFIED_NAME] == qn][:1]
    elif query in (cq.CYPHER_GRAPH_CALLERS, cq.CYPHER_GRAPH_CALLEES):
        callers = query == cq.CYPHER_GRAPH_CALLERS
        for src, dst, line, col, el, ec, argc, kws in CALLS:
            if (dst if callers else src) != qn:
                continue
            other = _by_qn(src if callers else dst)
            out.append(
                {
                    cs.KEY_LABEL: other[cs.KEY_LABEL],
                    cs.KEY_QUALIFIED_NAME: other[cs.KEY_QUALIFIED_NAME],
                    cs.KEY_PATH: other[cs.KEY_PATH],
                    cs.KEY_LINE: line,
                    cs.KEY_COL: col,
                    cs.KEY_END_LINE: el,
                    cs.KEY_END_COL: ec,
                    cs.KEY_ARG_COUNT: argc,
                    cs.KEY_KWARG_NAMES: kws,
                }
            )
    elif query == cq.CYPHER_GRAPH_IMPLEMENTORS:
        for src, dst, rel in INHERITS:
            if dst == qn:
                n = _by_qn(src)
                out.append(
                    {
                        cs.KEY_LABEL: n[cs.KEY_LABEL],
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: n[cs.KEY_PATH],
                        cs.KEY_REL_TYPE: rel,
                    }
                )
    elif query == cq.CYPHER_GRAPH_OVERRIDES:
        for src, dst in OVERRIDES:
            for a, b in ((src, dst), (dst, src)):
                if b == qn:
                    n = _by_qn(a)
                    out.append(
                        {
                            cs.KEY_LABEL: n[cs.KEY_LABEL],
                            cs.KEY_QUALIFIED_NAME: a,
                            cs.KEY_PATH: n[cs.KEY_PATH],
                            cs.KEY_REL_TYPE: "OVERRIDES",
                        }
                    )
    elif query == cq.CYPHER_GRAPH_IMPORTERS:
        for src, dst, line, col, el, ec, alias, name in IMPORTS:
            if dst == qn:
                n = _by_qn(src)
                out.append(
                    {
                        cs.KEY_QUALIFIED_NAME: src,
                        cs.KEY_PATH: n[cs.KEY_PATH],
                        cs.KEY_LINE: line,
                        cs.KEY_COL: col,
                        cs.KEY_END_LINE: el,
                        cs.KEY_END_COL: ec,
                        cs.KEY_ALIAS: alias,
                        cs.KEY_IMPORTED_NAME: name,
                    }
                )
    elif query == cq.CYPHER_DEAD_CODE_NODES:
        out = [
            dict(
                n,
                is_exported=False,
                overrides_external=False,
                rust_cfg_test_mods=[],
                rust_ungated_mods=[],
            )
            for n in NODES
        ]
    elif query == cq.CYPHER_DEAD_CODE_RELS:
        for src, dst, *_ in CALLS:
            out.append(
                {
                    cs.KEY_FROM_LABEL: _by_qn(src)[cs.KEY_LABEL],
                    cs.KEY_FROM_QN: src,
                    cs.KEY_REL_TYPE: "CALLS",
                    cs.KEY_TO_LABEL: _by_qn(dst)[cs.KEY_LABEL],
                    cs.KEY_TO_QN: dst,
                }
            )
    else:
        raise AssertionError(f"unexpected query: {query[:60]}")
    # Deliberately unsorted: the tools must sort.
    return list(reversed(out))


# --- resolve / definition ------------------------------------------------------


def test_resolve_orders_exact_then_suffix_then_name() -> None:
    rows = graph_query.resolve(fake_fetch_all, P, "go")
    assert [r["qualified_name"] for r in rows] == [
        f"{P}.app.Base.go",
        f"{P}.app.Child.go",
    ]
    rows = graph_query.resolve(fake_fetch_all, P, "Child.go")
    assert [r["qualified_name"] for r in rows] == [
        f"{P}.app.Child.go",
        f"{P}.app.Base.go",
    ]
    rows = graph_query.resolve(fake_fetch_all, P, f"{P}.app.run")
    assert rows[0]["qualified_name"] == f"{P}.app.run"
    assert rows[0]["label"] == "Function"
    assert graph_query.resolve(fake_fetch_all, P, "nothing") == []


def test_resolve_by_location_is_innermost_first() -> None:
    rows = graph_query.resolve(fake_fetch_all, P, "app.py:16")
    assert [r["qualified_name"] for r in rows] == [
        f"{P}.app.Base.go",
        f"{P}.app.Base",
        f"{P}.app",
    ]


def test_definition_reads_source_inside_the_repo(tmp_path: Path) -> None:
    (tmp_path / "util.py").write_text(
        "def helper(a):\n    '''Help.'''\n    return a\n\n", encoding="utf-8"
    )
    row = graph_query.definition(fake_fetch_all, P, f"{P}.util.helper", tmp_path)
    assert row["found"] is True
    assert row["path"] == "util.py"
    assert (row["start_line"], row["end_line"]) == (1, 4)
    assert row["docstring"] == "Help."
    assert row["source"] is not None and row["source"].startswith("def helper(a):")
    missing = graph_query.definition(fake_fetch_all, P, f"{P}.nope", tmp_path)
    assert missing["found"] is False
    assert missing["source"] is None


def test_definition_without_a_repo_root_keeps_the_span() -> None:
    row = graph_query.definition(fake_fetch_all, P, f"{P}.app.run", None)
    assert row["found"] and row["source"] is None and row["start_line"] == 3


# --- callers / callees -----------------------------------------------------------


def test_callers_returns_one_row_per_call_site() -> None:
    rows = graph_query.callers(fake_fetch_all, P, f"{P}.util.helper")
    assert [
        (r["qualified_name"], r["line"], r["col"], r["arg_count"], r["kwarg_names"])
        for r in rows
    ] == [
        (f"{P}.app.run", 4, 11, 1, []),
        (f"{P}.app.run", 6, 11, 2, ["b"]),
        (f"{P}.tests.test_app.setup", None, None, None, None),
    ]
    assert all(r["depth"] == 1 and r["through"] == f"{P}.util.helper" for r in rows)


def test_callers_depth_follows_callers_of_callers_once_each() -> None:
    rows = graph_query.callers(fake_fetch_all, P, f"{P}.util.helper", depth=3)
    assert [(r["depth"], r["qualified_name"], r["through"]) for r in rows] == [
        (1, f"{P}.app.run", f"{P}.util.helper"),
        (1, f"{P}.app.run", f"{P}.util.helper"),
        (1, f"{P}.tests.test_app.setup", f"{P}.util.helper"),
        (2, f"{P}.app.main", f"{P}.app.run"),
        (2, f"{P}.tests.test_app.test_run", f"{P}.app.run"),
        (3, f"{P}.tests.test_app.test_main", f"{P}.app.main"),
    ]


def test_callees_lists_the_sites_inside_a_function() -> None:
    rows = graph_query.callees(fake_fetch_all, P, f"{P}.app.main", depth=2)
    assert [(r["depth"], r["qualified_name"], r["line"]) for r in rows] == [
        (1, f"{P}.app.run", 12),
        (2, f"{P}.util.helper", 4),
        (2, f"{P}.util.helper", 6),
    ]


def test_walks_are_deterministic() -> None:
    first = graph_query.callers(fake_fetch_all, P, f"{P}.util.helper", depth=3)
    second = graph_query.callers(fake_fetch_all, P, f"{P}.util.helper", depth=3)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# --- implementors / overrides / importers / tests_reaching ------------------------


def test_implementors_and_overrides() -> None:
    assert graph_query.implementors(fake_fetch_all, P, f"{P}.app.Base") == [
        {
            "label": "Class",
            "qualified_name": f"{P}.app.Child",
            "path": "app.py",
            "relationship": "INHERITS",
        }
    ]
    assert [
        r["qualified_name"]
        for r in graph_query.overrides(fake_fetch_all, P, f"{P}.app.Base.go")
    ] == [f"{P}.app.Child.go"]
    assert [
        r["qualified_name"]
        for r in graph_query.overrides(fake_fetch_all, P, f"{P}.app.Child.go")
    ] == [f"{P}.app.Base.go"]


def test_importers_carry_the_statement_locations() -> None:
    rows = graph_query.importers(fake_fetch_all, P, f"{P}.util")
    assert rows == [
        {
            "module": f"{P}.app",
            "path": "app.py",
            "line": 1,
            "col": 0,
            "end_line": 1,
            "end_col": 27,
            "alias": "helper",
            "imported_name": "helper",
        },
        {
            "module": f"{P}.tests.test_app",
            "path": "tests/test_app.py",
            "line": 2,
            "col": 0,
            "end_line": 2,
            "end_col": 22,
            "alias": "util",
            "imported_name": "util",
        },
    ]


def test_importers_sort_by_line_before_column() -> None:
    """Position orders by line first, then column -- not the reverse.

    The permutation tests below pin the SENTINELS but not the field ORDER:
    their fixtures share a line, so swapping `line` and `col` in the key
    leaves their output byte-identical and they pass either way. A row
    earlier in the file but later in its line is what separates the two,
    and reading imports out of file order is a real (if quiet) regression.
    """

    def row(line: int, col: int, alias: str) -> ResultRow:
        return {
            cs.KEY_QUALIFIED_NAME: f"{P}.app",
            cs.KEY_PATH: "app.py",
            cs.KEY_LINE: line,
            cs.KEY_COL: col,
            cs.KEY_END_LINE: line,
            cs.KEY_END_COL: col + 5,
            cs.KEY_ALIAS: alias,
            cs.KEY_IMPORTED_NAME: alias,
        }

    # `first` is earlier in the file but at a LATER column than `second`.
    rows = [row(2, 1, "second"), row(1, 5, "first")]
    got = graph_query.importers(lambda _q, _p=None: list(rows), P, f"{P}.util")
    assert [r["alias"] for r in got] == ["first", "second"]


def test_importers_order_does_not_depend_on_the_fetch_order() -> None:
    """Column zero must stay distinct from a missing column in the sort key.

    `col or -1` maps BOTH `0` and `None` to `-1`, so two imports of the same
    module on the same line -- one at column 0, one with no recorded column --
    collide on the sort key and keep whatever order the database happened to
    return. Column 0 is the common case (an import at the start of a line),
    so the collision is reachable rather than theoretical.

    Asserting a specific output order would NOT catch this: Python's sort is
    stable, so colliding keys preserve the input order and the buggy code
    returns the expected list whenever the fixture is already in that order.
    What the defect actually breaks is that the output stops being a function
    of the rows alone, so the assertion is that two permutations of the same
    rows produce the same JSON.
    """
    rows = [
        {
            cs.KEY_QUALIFIED_NAME: f"{P}.app",
            cs.KEY_PATH: "app.py",
            cs.KEY_LINE: 1,
            cs.KEY_COL: None,
            cs.KEY_END_LINE: 1,
            cs.KEY_END_COL: 20,
            cs.KEY_ALIAS: "same",
            cs.KEY_IMPORTED_NAME: "same",
        },
        {
            cs.KEY_QUALIFIED_NAME: f"{P}.app",
            cs.KEY_PATH: "app.py",
            cs.KEY_LINE: 1,
            cs.KEY_COL: 0,
            cs.KEY_END_LINE: 1,
            cs.KEY_END_COL: 27,
            cs.KEY_ALIAS: "same",
            cs.KEY_IMPORTED_NAME: "same",
        },
    ]

    def fetch(order: list[ResultRow]) -> list[graph_query.ImporterRow]:
        return graph_query.importers(lambda _q, _p=None: list(order), P, f"{P}.util")

    forward = fetch(rows)
    reversed_ = fetch(list(reversed(rows)))
    assert json.dumps(forward) == json.dumps(reversed_)
    # The row with a real column 0 must sort after the one with no column,
    # rather than tying with it: -1 (absent) < 0 (column zero). `end_col`
    # identifies the rows because every field the sort key READS is equal
    # between them apart from `col` -- otherwise a tie-breaker would order
    # them and the assertion would hold even with the collision restored.
    assert [r["end_col"] for r in forward] == [20, 27]


def test_importers_absent_line_is_distinct_from_line_zero() -> None:
    """`line` needs the same sentinel discipline as `col`, one field up.

    The test above pins `col`, and nothing pinned `line`: no fixture gave it
    `None` beside a row with `line == 0`, so changing its sentinel to `0` --
    or reverting it to `or 0` -- passed every test in this file. That is the
    identical collision the column fix removed, on the field that outranks it.

    The branch is reachable: `_opt_int` yields `None` for any non-int the
    graph returns, so a row genuinely can arrive without a line.
    """

    def row(line: int | None, end_col: int) -> ResultRow:
        return {
            cs.KEY_QUALIFIED_NAME: f"{P}.app",
            cs.KEY_PATH: "app.py",
            cs.KEY_LINE: line,
            cs.KEY_COL: 0,
            cs.KEY_END_LINE: 1,
            cs.KEY_END_COL: end_col,
            cs.KEY_ALIAS: "same",
            cs.KEY_IMPORTED_NAME: "same",
        }

    # Every field the key READS is equal apart from `line`, so nothing else
    # can order the pair -- the same discipline the column fixture uses.
    rows = [row(None, 20), row(0, 27)]

    def fetch(order: list[ResultRow]) -> list[graph_query.ImporterRow]:
        return graph_query.importers(lambda _q, _p=None: list(order), P, f"{P}.util")

    forward = fetch(rows)
    assert json.dumps(forward) == json.dumps(fetch(list(reversed(rows))))
    # -1 (absent) sorts before 0 (line zero); folding them together would
    # leave the pair in whatever order the graph happened to return.
    assert [r["end_col"] for r in forward] == [20, 27]


def test_importers_co_located_on_one_statement_order_by_name() -> None:
    """Names from a single `from x import a, b` share module, line AND column.

    Position alone cannot separate them, so without a name tie-breaker the
    pair keeps the graph's arbitrary order. This is a distinct collision from
    the column-zero one above: that test still passes with the tie-breakers
    removed, and this one still passes with the column fix reverted.

    `alias` and `imported_name` deliberately DISAGREE, modelling
    `from x import b as y, a as z`. Setting them equal would tie the two
    fields together, and a fixture that ties them cannot see which one the key
    consults or in what order -- dropping either, or swapping the pair, would
    leave the output identical and the test green.
    """

    def row(alias: str, imported: str, end_col: int) -> ResultRow:
        return {
            cs.KEY_QUALIFIED_NAME: f"{P}.app",
            cs.KEY_PATH: "app.py",
            cs.KEY_LINE: 1,
            cs.KEY_COL: 0,
            cs.KEY_END_LINE: 1,
            cs.KEY_END_COL: end_col,
            cs.KEY_ALIAS: alias,
            cs.KEY_IMPORTED_NAME: imported,
        }

    # Alias order (y, z) is the REVERSE of imported-name order (b, a), so the
    # expected result holds only if `alias` is read first.
    rows = [row("z", "a", 30), row("y", "b", 20)]

    def fetch(order: list[ResultRow]) -> list[graph_query.ImporterRow]:
        return graph_query.importers(lambda _q, _p=None: list(order), P, f"{P}.util")

    assert [r["alias"] for r in fetch(rows)] == ["y", "z"]
    assert json.dumps(fetch(rows)) == json.dumps(fetch(list(reversed(rows))))

    # `imported_name` needs its own rows. Above, the aliases differ, so `alias`
    # separates the pair on its own and the second tie-breaker is never
    # consulted -- dropping `imported_name` from the key passes every
    # assertion above. Tying the alias is what makes it load-bearing, and the
    # case is real: `from x import a as z, b as z` is a redefinition, but the
    # graph holds both rows and must order them deterministically.
    tied = [row("z", "b", 30), row("z", "a", 20)]

    assert [r["imported_name"] for r in fetch(tied)] == ["a", "b"]
    assert json.dumps(fetch(tied)) == json.dumps(fetch(list(reversed(tied))))


def test_importers_group_by_importing_module_first() -> None:
    """`module` is the primary key, ahead of every positional field.

    Every other importers fixture uses a single importing module, so `module`
    never discriminates and dropping it from the key is a mutation they all
    pass through. It is not equivalent: dropping it loses per-module grouping,
    so importers are interleaved by position across modules instead of listed
    together.

    The ORDER assertion is what catches that; the permutation assertion below
    is a redundant guard here rather than the discriminating one. These rows
    sit at lines 1 and 9, so without `module` their keys are still distinct
    and the output stays deterministic -- wrong, but stable under
    permutation. Fetch-order dependence needs a genuine key COLLISION, which
    is what the sentinel and tie-breaker tests above supply.

    `zeta` imports at line 1 and `alpha` at line 9, so grouping by module is
    the only thing that can put `alpha` first -- ordering by position would
    invert the pair.
    """

    def row(module: str, line: int) -> ResultRow:
        return {
            cs.KEY_QUALIFIED_NAME: module,
            cs.KEY_PATH: f"{module.rsplit('.', 1)[-1]}.py",
            cs.KEY_LINE: line,
            cs.KEY_COL: 0,
            cs.KEY_END_LINE: line,
            cs.KEY_END_COL: 10,
            cs.KEY_ALIAS: "same",
            cs.KEY_IMPORTED_NAME: "same",
        }

    rows = [row(f"{P}.zeta", 1), row(f"{P}.alpha", 9)]

    def fetch(order: list[ResultRow]) -> list[graph_query.ImporterRow]:
        return graph_query.importers(lambda _q, _p=None: list(order), P, f"{P}.util")

    assert [r["module"] for r in fetch(rows)] == [f"{P}.alpha", f"{P}.zeta"]
    assert json.dumps(fetch(rows)) == json.dumps(fetch(list(reversed(rows))))


def test_tests_reaching_walks_backwards_to_test_symbols() -> None:
    rows = graph_query.tests_reaching(fake_fetch_all, P, f"{P}.util.helper")
    assert [(r["depth"], r["qualified_name"], r["through"]) for r in rows] == [
        (1, f"{P}.tests.test_app.setup", f"{P}.util.helper"),
        (2, f"{P}.tests.test_app.test_run", f"{P}.app.run"),
        (3, f"{P}.tests.test_app.test_main", f"{P}.app.main"),
    ]
    # Production callers on the way are not tests and are not listed.
    assert all(
        "app.run" not in r["qualified_name"] and "app.main" not in r["qualified_name"]
        for r in rows
    )
    assert graph_query.tests_reaching(fake_fetch_all, P, f"{P}.app.Base") == []


# --- MCP tools --------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> MCPToolsRegistry:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=fake_fetch_all)
    ingestor.list_projects.return_value = [P, "other"]
    with patch("codebase_rag.mcp.tools.load_parsers", return_value=({}, {})):
        reg = MCPToolsRegistry(
            project_root=str(tmp_path), ingestor=ingestor, cypher_gen=MagicMock()
        )
    return reg


async def test_mcp_tools_are_registered_as_json_tools(
    registry: MCPToolsRegistry,
) -> None:
    for name in (
        cs.MCPToolName.RESOLVE,
        cs.MCPToolName.DEFINITION,
        cs.MCPToolName.CALLERS,
        cs.MCPToolName.CALLEES,
        cs.MCPToolName.IMPLEMENTORS,
        cs.MCPToolName.OVERRIDES,
        cs.MCPToolName.IMPORTERS,
        cs.MCPToolName.TESTS_REACHING,
    ):
        meta = registry._tools[name]
        assert meta.returns_json is True
        assert cs.MCPParamName.PROJECT in meta.input_schema["properties"]
    assert (
        registry._tools[cs.MCPToolName.CALLERS].input_schema["properties"][
            cs.MCPParamName.DEPTH
        ]["type"]
        == "integer"
    )


async def test_mcp_callers_scopes_to_the_given_project(
    registry: MCPToolsRegistry,
) -> None:
    rows = await registry.callers(f"{P}.util.helper", depth=2, project=P)
    assert isinstance(rows, list) and len(rows) == 5
    assert rows[0]["line"] == 4


async def test_mcp_unknown_project_is_refused_before_any_query(
    registry: MCPToolsRegistry,
) -> None:
    result = await registry.resolve("helper", project="typo")
    assert isinstance(result, dict) and "typo" in result["error"]
    registry.ingestor.fetch_all.assert_not_called()


async def test_mcp_depth_is_clamped(registry: MCPToolsRegistry) -> None:
    rows = await registry.callees(f"{P}.app.main", depth=99, project=P)
    assert (
        isinstance(rows, list)
        and max(r["depth"] for r in rows) <= cs.GRAPH_QUERY_MAX_DEPTH
    )


async def test_mcp_errors_are_reported_not_raised(registry: MCPToolsRegistry) -> None:
    registry.ingestor.fetch_all.side_effect = RuntimeError("down")
    result = await registry.importers(f"{P}.util", project=P)
    assert isinstance(result, dict) and "down" in result["error"]


def test_query_code_graph_description_points_at_the_deterministic_tools() -> None:
    from codebase_rag.tools import tool_descriptions as td

    text = td.MCP_TOOLS[cs.MCPToolName.QUERY_CODE_GRAPH]
    for name in ("resolve", "definition", "callers", "tests_reaching"):
        assert name in text


# --- cgr graph --------------------------------------------------------------------


async def test_mcp_definition_reads_source_only_for_the_servers_own_project(
    registry: MCPToolsRegistry, tmp_path: Path
) -> None:
    # The graph may hold several projects; a definition of another project
    # carries a relative path that can also exist under this server's root
    # and must not be read from here.
    (tmp_path / "util.py").write_text(
        "def helper(a):\n    return a\n", encoding="utf-8"
    )
    foreign = await registry.definition(f"{P}.util.helper", project=P)
    assert foreign["found"] is True
    assert foreign["source"] is None
    with patch.object(sys.modules[__name__], "PROJECT_ROOT", str(tmp_path)):
        own = await registry.definition(f"{P}.util.helper", project=P)
    assert own["source"] is not None and own["source"].startswith("def helper(a):")


def test_cli_definition_reads_source_only_for_the_repos_own_project(
    tmp_path: Path,
) -> None:
    (tmp_path / "util.py").write_text(
        "def helper(a):\n    return a\n", encoding="utf-8"
    )
    args = [
        "definition",
        f"{P}.util.helper",
        "--project",
        P,
        "--repo-path",
        str(tmp_path),
    ]
    with patch("codebase_rag.main.connect_memgraph", return_value=_mock_connect()):
        foreign = CliRunner().invoke(graph_cli, args)
        with patch.object(sys.modules[__name__], "PROJECT_ROOT", str(tmp_path)):
            own = CliRunner().invoke(graph_cli, args)
    assert foreign.exit_code == 0, foreign.output
    assert json.loads(foreign.output)["source"] is None
    assert own.exit_code == 0, own.output
    assert json.loads(own.output)["source"].startswith("def helper(a):")


def _mock_connect() -> MagicMock:
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=fake_fetch_all)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    return ingestor


def test_cli_callers_prints_sorted_json(tmp_path: Path) -> None:
    with patch("codebase_rag.main.connect_memgraph", return_value=_mock_connect()):
        result = CliRunner().invoke(
            graph_cli,
            [
                "callers",
                f"{P}.util.helper",
                "--depth",
                "2",
                "--project",
                P,
                "--repo-path",
                str(tmp_path),
            ],
        )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [r["qualified_name"] for r in rows][:2] == [f"{P}.app.run", f"{P}.app.run"]


def test_cli_depth_is_bounded(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        graph_cli, ["callers", "x", "--depth", "9", "--repo-path", str(tmp_path)]
    )
    assert result.exit_code == 2


def test_cli_every_subcommand_is_registered() -> None:
    assert set(graph_cli.commands) == {
        "resolve",
        "definition",
        "callers",
        "callees",
        "implementors",
        "overrides",
        "importers",
        "tests-reaching",
    }
