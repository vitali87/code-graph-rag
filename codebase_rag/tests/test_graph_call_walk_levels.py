# `cgr graph callers/callees --depth N`, and the MCP `callers` / `callees`
# tools that share the walk (issue #2597). The walk ran one Cypher query per
# FRONTIER NODE, and that query found its node by `qualified_name` with no
# label, so Memgraph could not use the (:Function {qualified_name}) /
# (:Method {qualified_name}) indexes and scanned the whole shared graph each
# time: depth 2 on django was 886 queries and 154 s. The fake graph below is
# that walk in miniature (callers of several labels, a caller reaching two
# frontier nodes, a cycle back to the start, another project's node between
# this project's), and every test pins what the walk asks the graph or what
# it answers.
from __future__ import annotations

import json
import re
from collections.abc import Callable

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_query
from codebase_rag.types_defs import RELATIONSHIP_SCHEMAS, PropertyDict, ResultRow

P = "proj"
# A registered project whose name extends this one: its rows start with
# `proj.` and pass the Cypher prefix filter, so only ownership drops them.
EXTRA = "proj.extra"

TARGET = f"{P}.urls.base.reverse"
INDEX = f"{P}.app.views.index"
DETAIL = f"{P}.app.views.Detail.get"
URLS = f"{P}.app.urls"
TEST_INDEX = f"{P}.tests.test_views.test_index"
TEST_URLS = f"{P}.tests.test_urls.test_root"
RUNNER = f"{P}.tests.runner.run_all"
HOOK = f"{EXTRA}.app.hook"
VIA_HOOK = f"{P}.plugins.via_hook"

LABELS = {
    TARGET: "Function",
    INDEX: "Function",
    DETAIL: "Method",
    URLS: "Module",
    TEST_INDEX: "Function",
    TEST_URLS: "Function",
    RUNNER: "Function",
    HOOK: "Function",
    VIA_HOOK: "Function",
}

# (caller qn, callee qn, line): one CALLS edge, one site.
CALLS: list[tuple[str, str, int]] = [
    (INDEX, TARGET, 10),
    # A second site of the same caller: two rows, one hop.
    (INDEX, TARGET, 20),
    (DETAIL, TARGET, 11),
    # A module-level call: the caller is the Module.
    (URLS, TARGET, 12),
    # Another project's caller of this project's function (issue #1982).
    (HOOK, TARGET, 2),
    # This project's caller reachable ONLY through the foreign hook.
    (VIA_HOOK, HOOK, 3),
    # A caller of a frontier node that is itself in the same frontier.
    (DETAIL, INDEX, 30),
    # One test calling two frontier nodes: a row through each.
    (TEST_INDEX, INDEX, 5),
    (TEST_INDEX, DETAIL, 6),
    (TEST_URLS, URLS, 7),
    (RUNNER, TEST_INDEX, 3),
    (RUNNER, TEST_URLS, 4),
    # A cycle back to the start.
    (TARGET, RUNNER, 70),
]


def _path(qn: str) -> str:
    return qn.removeprefix(f"{P}.").replace(".", "/") + ".py"


def _fetch_for(
    callers: bool,
    calls: list[tuple[str, str, int]] = CALLS,
    log: list[tuple[str, PropertyDict]] | None = None,
    reverse_rows: bool = True,
) -> Callable[[str, PropertyDict | None], list[ResultRow]]:
    """The graph answering the walk's reads in one direction, with the
    project-prefix filter their Cypher applies. A read naming one node
    (`qn`) is answered for it; a read naming a whole frontier (`qns`) for
    every node in it, each row carrying the frontier node it hangs off."""
    prefix = f"{P}."
    through_key = cs.KEY_TO_QN if callers else cs.KEY_FROM_QN

    def fetch(query: str, params: PropertyDict | None = None) -> list[ResultRow]:
        if query == cq.CYPHER_LIST_PROJECTS:
            return [{cs.KEY_NAME: name} for name in (P, EXTRA)]
        p = params or {}
        assert p.get(cs.KEY_PROJECT_PREFIX) == prefix, "every read is scoped"
        if log is not None:
            log.append((query, p))
        batched = cs.KEY_QNS in p
        frontier = {str(qn) for qn in _asked(p)}
        out: list[ResultRow] = []
        for caller, callee, line in calls:
            this, other = (callee, caller) if callers else (caller, callee)
            if this not in frontier or not other.startswith(prefix):
                continue
            row: ResultRow = {
                cs.KEY_LABEL: LABELS[other],
                cs.KEY_QUALIFIED_NAME: other,
                cs.KEY_PATH: _path(caller),
                cs.KEY_CALLEE_PATH: _path(callee),
                cs.KEY_LINE: line,
                cs.KEY_COL: 4,
                cs.KEY_END_LINE: line,
                cs.KEY_END_COL: 20,
                cs.KEY_ARG_COUNT: 1,
                cs.KEY_KWARG_NAMES: [],
                cs.KEY_RESOLUTION: None,
            }
            if batched:
                row[through_key] = this
            out.append(row)
        # Deliberately unsorted: the walk must order its own output.
        return list(reversed(out)) if reverse_rows else out

    return fetch


def _asked(params: PropertyDict) -> list[str]:
    """The names one read looks up: its whole frontier, or its one node."""
    qns = params[cs.KEY_QNS] if cs.KEY_QNS in params else [params[cs.KEY_QN]]
    assert isinstance(qns, list)
    return [str(qn) for qn in qns]


def _hops(rows: list[graph_query.CallSiteRow]) -> list[tuple[int, str, str, int]]:
    return [
        (r["depth"], r["through"], r["qualified_name"], r["line"] or 0) for r in rows
    ]


# --- one query per level, not per node --------------------------------------------


def test_callers_ask_once_per_level_for_the_whole_frontier() -> None:
    log: list[tuple[str, PropertyDict]] = []
    graph_query.callers(_fetch_for(callers=True, log=log), P, TARGET, depth=5)
    assert [_asked(p) for _q, p in log] == [
        [TARGET],
        [URLS, DETAIL, INDEX],
        [TEST_URLS, TEST_INDEX],
        [RUNNER],
    ]
    # The start's own read, then one batched read per later level.
    assert [q for q, _p in log] == [
        cq.CYPHER_GRAPH_CALLERS,
        cq.CYPHER_GRAPH_CALLERS_OF,
        cq.CYPHER_GRAPH_CALLERS_OF,
        cq.CYPHER_GRAPH_CALLERS_OF,
    ]


def test_callees_ask_once_per_level_for_the_whole_frontier() -> None:
    log: list[tuple[str, PropertyDict]] = []
    graph_query.callees(_fetch_for(callers=False, log=log), P, RUNNER, depth=5)
    assert [_asked(p) for _q, p in log] == [
        [RUNNER],
        [TEST_URLS, TEST_INDEX],
        [URLS, DETAIL, INDEX],
        [TARGET],
    ]
    assert [q for q, _p in log] == [
        cq.CYPHER_GRAPH_CALLEES,
        cq.CYPHER_GRAPH_CALLEES_OF,
        cq.CYPHER_GRAPH_CALLEES_OF,
        cq.CYPHER_GRAPH_CALLEES_OF,
    ]


# --- the looked-up node is found through the label + qualified_name indexes -------


def _calls_endpoint_labels() -> set[str]:
    """Every label the schema lets a CALLS edge start or end on: a frontier
    node past depth 1 is the far end of the previous hop's edge, so the
    lookup must cover both ends whichever direction it walks."""
    labels: set[str] = set()
    for schema in RELATIONSHIP_SCHEMAS:
        if schema.rel_type == cs.RelationshipType.CALLS:
            labels |= {label.value for label in (*schema.sources, *schema.targets)}
    return labels


@pytest.mark.parametrize(
    ("name", "looked_up", "other", "key"),
    [
        ("CYPHER_GRAPH_CALLERS", "callee", "caller", f"${cs.KEY_QN}"),
        ("CYPHER_GRAPH_CALLEES", "caller", "callee", f"${cs.KEY_QN}"),
        ("CYPHER_GRAPH_CALLERS_OF", "callee", "caller", cs.KEY_QN),
        ("CYPHER_GRAPH_CALLEES_OF", "caller", "callee", cs.KEY_QN),
    ],
)
def test_the_looked_up_node_is_matched_by_label_and_qualified_name(
    name: str, looked_up: str, other: str, key: str
) -> None:
    query = getattr(cq, name)
    # Its own MATCH, so the planner starts there from the label indexes
    # rather than scanning every node for the unlabelled far end.
    lookup = re.search(
        rf"MATCH \({looked_up}:([A-Za-z|]+) \{{qualified_name: (\$?\w+)\}}\)", query
    )
    assert lookup is not None, query
    assert _calls_endpoint_labels() <= set(lookup.group(1).split("|"))
    assert lookup.group(2) == key
    if not key.startswith("$"):
        assert f"UNWIND ${cs.KEY_QNS} AS {key}" in query
    # The far end keeps its project filter, and nothing compares the
    # looked-up node's qualified_name outside the indexed lookup.
    assert f"{other}.qualified_name STARTS WITH ${cs.KEY_PROJECT_PREFIX}" in query
    assert f"{looked_up}.qualified_name =" not in query


# --- what must not change ------------------------------------------------------------


def test_callers_rows_are_the_sites_of_each_hop() -> None:
    rows = graph_query.callers(_fetch_for(callers=True), P, TARGET, depth=5)
    assert _hops(rows) == [
        (1, TARGET, URLS, 12),
        (1, TARGET, DETAIL, 11),
        (1, TARGET, INDEX, 10),
        (1, TARGET, INDEX, 20),
        (2, URLS, TEST_URLS, 7),
        (2, DETAIL, TEST_INDEX, 6),
        # Detail.get is already listed, so it is a site here and not a hop.
        (2, INDEX, DETAIL, 30),
        (2, INDEX, TEST_INDEX, 5),
        (3, TEST_URLS, RUNNER, 4),
        (3, TEST_INDEX, RUNNER, 3),
        # The cycle: the start's own site is listed, the start never re-walked.
        (4, RUNNER, TARGET, 70),
    ]
    assert rows[0] == {
        "label": "Module",
        "qualified_name": URLS,
        "path": _path(URLS),
        "callee_path": _path(TARGET),
        "line": 12,
        "col": 4,
        "end_line": 12,
        "end_col": 20,
        "arg_count": 1,
        "kwarg_names": [],
        "resolution": None,
        "depth": 1,
        "through": TARGET,
    }


def test_callees_rows_are_the_sites_of_each_hop() -> None:
    rows = graph_query.callees(_fetch_for(callers=False), P, RUNNER, depth=5)
    assert _hops(rows) == [
        (1, RUNNER, TEST_URLS, 4),
        (1, RUNNER, TEST_INDEX, 3),
        (2, TEST_URLS, URLS, 7),
        (2, TEST_INDEX, DETAIL, 6),
        (2, TEST_INDEX, INDEX, 5),
        (3, URLS, TARGET, 12),
        (3, DETAIL, INDEX, 30),
        (3, DETAIL, TARGET, 11),
        (3, INDEX, TARGET, 10),
        (3, INDEX, TARGET, 20),
        (4, TARGET, RUNNER, 70),
    ]
    # `path` is the site's file (the caller's), `callee_path` the callee's.
    detail = next(r for r in rows if r["through"] == DETAIL and r["line"] == 11)
    assert (detail["path"], detail["callee_path"]) == (_path(DETAIL), _path(TARGET))


def test_another_projects_node_is_neither_listed_nor_a_hop() -> None:
    rows = graph_query.callers(_fetch_for(callers=True), P, TARGET, depth=5)
    names = {r["qualified_name"] for r in rows} | {r["through"] for r in rows}
    assert HOOK not in names
    # Calls the target only through the foreign hook.
    assert VIA_HOOK not in names
    # The other way round: the hook is all `via_hook` calls, so nothing.
    assert graph_query.callees(_fetch_for(callers=False), P, VIA_HOOK, depth=5) == []


def test_depth_one_is_the_single_node_read_of_the_start() -> None:
    # The default depth, and what rename collects its sites with.
    for walk, callers, read in (
        (graph_query.callers, True, cq.CYPHER_GRAPH_CALLERS),
        (graph_query.callees, False, cq.CYPHER_GRAPH_CALLEES),
    ):
        log: list[tuple[str, PropertyDict]] = []
        walk(_fetch_for(callers=callers, log=log), P, TARGET)
        assert log == [(read, {cs.KEY_PROJECT_PREFIX: f"{P}.", cs.KEY_QN: TARGET})]


def test_depth_stops_at_the_requested_hop() -> None:
    log: list[tuple[str, PropertyDict]] = []
    rows = graph_query.callers(_fetch_for(callers=True, log=log), P, TARGET, depth=2)
    assert max(r["depth"] for r in rows) == 2
    assert len(log) == 2


def test_an_unknown_name_answers_empty_after_one_query() -> None:
    log: list[tuple[str, PropertyDict]] = []
    fetch = _fetch_for(callers=True, log=log)
    assert graph_query.callers(fetch, P, f"{P}.nope", depth=5) == []
    fetch = _fetch_for(callers=False, log=log)
    assert graph_query.callees(fetch, P, f"{P}.nope", depth=5) == []
    assert len(log) == 2


def test_the_output_does_not_depend_on_the_row_order() -> None:
    for walk, start, callers in (
        (graph_query.callers, TARGET, True),
        (graph_query.callees, RUNNER, False),
    ):
        forward = walk(_fetch_for(callers, reverse_rows=False), P, start, 5)
        backward = walk(_fetch_for(callers, list(reversed(CALLS))), P, start, 5)
        assert json.dumps(forward) == json.dumps(backward)
