# Issue #2460: a `callees` row paired the callee's definition file (`path`)
# with the call site's `line`/`col`, which live in the CALLER's file, so
# `path:line` landed on unrelated code. Every call-site row now follows the
# `callers` contract: `path` is the file holding `line`/`col` (a call site is
# always inside its caller), and `callee_path` is where the invoked symbol is
# defined. The fixed Cypher is answered by the evals emulator, which mirrors
# the production reads query by query.
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.constants import graph as cs
from codebase_rag.graph_cli import cli as graph_cli
from evals.cgr_graph import _StatefulIngestor

P = "proj"
_FN = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_EXT = cs.NodeLabel.EXTERNAL_MODULE.value
_CALLS = cs.RelationshipType.CALLS.value

REPORT = f"{P}.app.report.report"
TOTAL = f"{P}.app.shapes.total"
INIT = f"{P}.app.shapes.Square.__init__"
ROUND2 = f"{P}.app.helpers.round2"


def _node(
    store: _StatefulIngestor, label: str, qn: str, path: str | None = None
) -> None:
    props: dict[str, str] = {
        cs.NODE_UNIQUE_CONSTRAINTS[label]: qn,
        cs.KEY_QUALIFIED_NAME: qn,
    }
    if path is not None:
        props[cs.KEY_PATH] = path
    store.ensure_node_batch(label, props)


def _call(
    store: _StatefulIngestor,
    src: tuple[str, str],
    dst: tuple[str, str],
    site: tuple[int, int, int, int] | None,
) -> None:
    props = (
        None
        if site is None
        else {
            cs.KEY_LINE: site[0],
            cs.KEY_COL: site[1],
            cs.KEY_END_LINE: site[2],
            cs.KEY_END_COL: site[3],
        }
    )
    store.ensure_relationship_batch(
        (src[0], cs.NODE_UNIQUE_CONSTRAINTS[src[0]], src[1]),
        _CALLS,
        (dst[0], cs.NODE_UNIQUE_CONSTRAINTS[dst[0]], dst[1]),
        props,
    )


def _store() -> _StatefulIngestor:
    """The issue's repro: `report()` in app/report.py calls `total` and
    `Square(2)` (both defined in app/shapes.py) on line 5; `total` in turn
    calls `round2` from app/helpers.py on its own line 9."""
    store = _StatefulIngestor()
    store.ensure_node_batch(
        cs.NodeLabel.PROJECT.value, {cs.KEY_NAME: P, cs.KEY_QUALIFIED_NAME: P}
    )
    _node(store, _FN, REPORT, "app/report.py")
    _node(store, _FN, TOTAL, "app/shapes.py")
    _node(store, _METHOD, INIT, "app/shapes.py")
    _node(store, _FN, ROUND2, "app/helpers.py")
    _call(store, (_FN, REPORT), (_FN, TOTAL), (5, 11, 5, 29))
    _call(store, (_FN, REPORT), (_METHOD, INIT), (5, 18, 5, 27))
    _call(store, (_FN, TOTAL), (_FN, ROUND2), (9, 11, 9, 20))
    return store


_Shape = tuple[int, str, str, str | None, int | None, str | None]


def _shape(rows: list[graph_query.CallSiteRow]) -> list[_Shape]:
    return [
        (
            r["depth"],
            r["qualified_name"],
            r["through"],
            r["path"],
            r["line"],
            r["callee_path"],
        )
        for r in rows
    ]


# --- the reported bug -------------------------------------------------------------


def test_callees_path_is_the_file_that_holds_the_call_site() -> None:
    rows = graph_query.callees(_store().fetch_all, P, REPORT)
    assert [(r["qualified_name"], r["path"], r["line"]) for r in rows] == [
        (INIT, "app/report.py", 5),
        (TOTAL, "app/report.py", 5),
    ]


def test_callees_rows_name_the_callee_definition_file_separately() -> None:
    rows = graph_query.callees(_store().fetch_all, P, REPORT)
    assert [(r["qualified_name"], r["callee_path"]) for r in rows] == [
        (INIT, "app/shapes.py"),
        (TOTAL, "app/shapes.py"),
    ]


def test_transitive_callees_site_is_in_the_through_nodes_file() -> None:
    """At depth 2 the site lives in the depth-1 callee's body, so its file
    is `through`'s file, not the report's and not the callee's."""
    rows = graph_query.callees(_store().fetch_all, P, REPORT, depth=2)
    assert _shape(rows) == [
        (1, INIT, REPORT, "app/report.py", 5, "app/shapes.py"),
        (1, TOTAL, REPORT, "app/report.py", 5, "app/shapes.py"),
        (2, ROUND2, TOTAL, "app/shapes.py", 9, "app/helpers.py"),
    ]


def test_callers_rows_name_the_callee_definition_file() -> None:
    rows = graph_query.callers(_store().fetch_all, P, ROUND2, depth=2)
    assert _shape(rows) == [
        (1, TOTAL, ROUND2, "app/shapes.py", 9, "app/helpers.py"),
        (2, REPORT, TOTAL, "app/report.py", 5, "app/shapes.py"),
    ]


def test_cli_callees_json_puts_path_and_line_on_the_call(tmp_path: Path) -> None:
    store = _store()
    ingestor = MagicMock()
    ingestor.fetch_all = MagicMock(side_effect=store.fetch_all)
    ingestor.__enter__ = MagicMock(return_value=ingestor)
    ingestor.__exit__ = MagicMock(return_value=False)
    args = ["callees", REPORT, "--depth", "2", "--project", P]
    with patch("codebase_rag.cli_runtime.connect_memgraph", return_value=ingestor):
        result = CliRunner().invoke(graph_cli, [*args, "--repo-path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [(r["path"], r["line"], r["col"], r["callee_path"]) for r in rows] == [
        ("app/report.py", 5, 18, "app/shapes.py"),
        ("app/report.py", 5, 11, "app/shapes.py"),
        ("app/shapes.py", 9, 11, "app/helpers.py"),
    ]


# --- what must not change ---------------------------------------------------------


def test_callers_path_is_still_the_callers_file() -> None:
    rows = graph_query.callers(_store().fetch_all, P, TOTAL)
    assert [(r["qualified_name"], r["path"], r["line"], r["col"]) for r in rows] == [
        (REPORT, "app/report.py", 5, 11)
    ]


def test_callees_keep_the_callee_as_the_row_and_the_caller_as_through() -> None:
    rows = graph_query.callees(_store().fetch_all, P, REPORT)
    assert [(r["qualified_name"], r["through"], r["label"]) for r in rows] == [
        (INIT, REPORT, _METHOD),
        (TOTAL, REPORT, _FN),
    ]


def test_a_siteless_edge_keeps_null_positions_and_both_files() -> None:
    store = _store()
    setup = f"{P}.tests.test_report.setup"
    _node(store, _FN, setup, "tests/test_report.py")
    _call(store, (_FN, setup), (_FN, TOTAL), None)
    rows = graph_query.callees(store.fetch_all, P, setup)
    assert _shape(rows) == [
        (1, TOTAL, setup, "tests/test_report.py", None, "app/shapes.py")
    ]
    assert (rows[0]["col"], rows[0]["end_line"], rows[0]["end_col"]) == (
        None,
        None,
        None,
    )


def test_a_callee_without_a_file_is_null_not_the_callers_file() -> None:
    store = _store()
    bare = f"{P}.app.gen.generated"
    _node(store, _FN, bare)
    _call(store, (_FN, REPORT), (_FN, bare), (4, 4, 4, 15))
    rows = graph_query.callees(store.fetch_all, P, REPORT)
    by_qn = {r["qualified_name"]: r for r in rows}
    assert by_qn[bare]["path"] == "app/report.py"
    assert by_qn[bare]["callee_path"] is None


def test_an_external_callee_is_still_not_a_row() -> None:
    store = _store()
    _node(store, _EXT, "requests")
    _call(store, (_FN, REPORT), (_EXT, "requests"), (3, 4, 3, 20))
    rows = graph_query.callees(store.fetch_all, P, REPORT)
    assert {r["qualified_name"] for r in rows} == {INIT, TOTAL}


def test_the_queries_return_the_site_file_and_the_callee_file() -> None:
    """The emulator mirrors production, so pin the production Cypher too:
    both reads take `path` from the caller, the node the site lives in."""
    for query in (cq.CYPHER_GRAPH_CALLERS, cq.CYPHER_GRAPH_CALLEES):
        assert "caller.path AS path" in query
        assert "callee.path AS callee_path" in query
