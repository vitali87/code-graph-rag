"""Unit tests for carrying trace-derived CALLS edges across a re-parse (issue #2429).

The end-to-end behaviour against a real Memgraph lives in
`integration/test_trace_edges_survive_reparse_e2e.py`; these pin the carry's
own decisions (what is kept, re-classified, graded stale or dropped) and the
updater's failure posture without a database.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.cypher_queries import (
    CYPHER_TRACE_CARRY_ENDPOINTS,
    CYPHER_TRACE_CARRY_STATIC_PAIRS,
    CYPHER_TRACE_CONFIRM_CALLS,
    CYPHER_TRACE_EDGES_AT_PATHS,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.trace.carry import (
    CapturedTraceEdge,
    capture_trace_edges,
    carry_trace_edges,
)
from codebase_rag.trace.resolution import ResolvedFrame
from codebase_rag.types_defs import (
    PropertyDict,
    PropertyParams,
    PropertyValue,
    ResultRow,
)

_PREFIX = "proj."
_CALLER = f"{_PREFIX}pkg.a.dispatch"
_CALLEE = f"{_PREFIX}pkg.b.handle"
_FUNCTION = cs.NodeLabel.FUNCTION.value

type _Spec = tuple[str, str, PropertyValue]


class _Store:
    """A query-capable ingestor answering the three carry queries from lists."""

    def __init__(
        self,
        captured: list[ResultRow] | None = None,
        endpoints: list[ResultRow] | None = None,
        static_pairs: list[ResultRow] | None = None,
        fail_reads: bool = False,
    ) -> None:
        self.captured = captured or []
        self.endpoints = endpoints or []
        self.static_pairs = static_pairs or []
        self.fail_reads = fail_reads
        self.reads: list[tuple[str, PropertyParams | None]] = []
        self.writes: list[tuple[str, PropertyParams | None]] = []
        self.edges: list[tuple[_Spec, str, _Spec, PropertyDict | None]] = []
        self.flushed = 0

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        self.reads.append((query, params))
        if self.fail_reads:
            raise RuntimeError("store down")
        if query == CYPHER_TRACE_EDGES_AT_PATHS:
            return self.captured
        if query == CYPHER_TRACE_CARRY_ENDPOINTS:
            wanted = (params or {}).get(cs.KEY_QNS) or []
            return [r for r in self.endpoints if r[cs.KEY_QUALIFIED_NAME] in wanted]
        if query == CYPHER_TRACE_CARRY_STATIC_PAIRS:
            return self.static_pairs
        raise AssertionError(f"unexpected query: {query}")

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        self.writes.append((query, params))

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        raise AssertionError("the carry must not create nodes")

    def ensure_relationship_batch(
        self,
        from_spec: _Spec,
        rel_type: str,
        to_spec: _Spec,
        properties: PropertyDict | None = None,
    ) -> None:
        self.edges.append((from_spec, rel_type, to_spec, properties))

    def flush_all(self) -> None:
        self.flushed += 1


def _captured_row(
    caller: str = _CALLER,
    callee: str = _CALLEE,
    *,
    from_hash: str | None = "ah1:caller",
    to_hash: str | None = "ah1:callee",
    **props: PropertyValue,
) -> ResultRow:
    edge_props: dict[str, PropertyValue] = {
        cs.TRACE_PROP_DYNAMIC: True,
        cs.TRACE_PROP_CALL_COUNT: 3,
        cs.TRACE_PROP_WORKLOADS: ["t::one"],
        cs.TRACE_PROP_WORKLOAD_COUNT: 1,
        cs.TRACE_PROP_RECEIVER_TYPES: [],
        cs.TRACE_PROP_SAMPLED: False,
        cs.TRACE_PROP_STATIC_MISSED: True,
        cs.KEY_RESOLUTION: cs.EdgeResolution.DYNAMIC.value,
        cs.TRACE_PROP_STALE: False,
        **props,
    }
    return {
        cs.KEY_FROM_LABEL: _FUNCTION,
        cs.KEY_FROM_QN: caller,
        cs.KEY_FROM_PATH: "pkg/a.py",
        cs.KEY_FROM_HASH: from_hash,
        cs.KEY_TO_LABEL: _FUNCTION,
        cs.KEY_TO_QN: callee,
        cs.KEY_TO_PATH: "pkg/b.py",
        cs.KEY_TO_HASH: to_hash,
        cs.KEY_PROPS: edge_props,
    }


def _endpoint_row(qualified_name: str, path: str, anchor_hash: str | None) -> ResultRow:
    return {
        cs.KEY_LABEL: _FUNCTION,
        cs.KEY_QUALIFIED_NAME: qualified_name,
        cs.KEY_PATH: path,
        cs.KEY_START_LINE: 1,
        cs.KEY_END_LINE: 2,
        cs.KEY_ANCHOR_HASH: anchor_hash,
    }


def _both_endpoints(
    caller_hash: str | None = "ah1:caller", callee_hash: str | None = "ah1:callee"
) -> list[ResultRow]:
    return [
        _endpoint_row(_CALLER, "pkg/a.py", caller_hash),
        _endpoint_row(_CALLEE, "pkg/b.py", callee_hash),
    ]


def _capture(store: _Store) -> list[CapturedTraceEdge]:
    return capture_trace_edges(store, ["pkg/a.py"], _PREFIX)


def _written(store: _Store) -> PropertyDict:
    ((_frm, rel, _to, props),) = store.edges
    assert rel == cs.RelationshipType.CALLS
    assert props is not None
    return props


def test_capture_scopes_its_read_to_the_paths_and_project() -> None:
    store = _Store()

    capture_trace_edges(store, ["pkg/a.py", "pkg/b.py"], _PREFIX)

    assert store.reads == [
        (
            CYPHER_TRACE_EDGES_AT_PATHS,
            {cs.CYPHER_PARAM_PATHS: ["pkg/a.py", "pkg/b.py"], cs.KEY_PREFIX: _PREFIX},
        )
    ]


def test_capture_folds_a_confirmed_pairs_sites_into_one_observation() -> None:
    # A confirmed pair carries the observation on each static site edge.
    store = _Store(
        captured=[
            _captured_row(line=3, col=4),
            _captured_row(line=7, col=4, dynamic_stale=True),
        ]
    )

    (edge,) = _capture(store)

    assert edge.caller == ResolvedFrame(_FUNCTION, _CALLER)
    assert edge.callee == ResolvedFrame(_FUNCTION, _CALLEE)
    assert edge.stale is True
    # Only the runtime observation travels; the site, resolution and
    # static_missed are re-derived against the re-parsed graph.
    assert set(edge.observation) == set(cs.TRACE_CARRIED_PROPS)
    assert edge.observation[cs.TRACE_PROP_CALL_COUNT] == 3


def test_capture_skips_rows_without_both_endpoints() -> None:
    malformed = _captured_row()
    malformed[cs.KEY_TO_QN] = None

    assert _capture(_Store(captured=[malformed])) == []


def test_carry_confirms_a_pair_the_reparse_gave_a_static_edge(tmp_path: Path) -> None:
    store = _Store(
        captured=[_captured_row()],
        endpoints=_both_endpoints(),
        static_pairs=[{cs.KEY_FROM_QN: _CALLER, cs.KEY_TO_QN: _CALLEE}],
    )

    summary = carry_trace_edges(store, _capture(store), tmp_path)

    assert (summary.carried, summary.newly_stale, summary.dropped) == (1, 0, 0)
    assert store.writes == [
        (
            CYPHER_TRACE_CONFIRM_CALLS,
            {
                cs.KEY_FROM_QN: _CALLER,
                cs.KEY_TO_QN: _CALLEE,
                cs.KEY_RESOLUTION: cs.EdgeResolution.TRACE_CONFIRMED,
            },
        )
    ]
    props = _written(store)
    assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.TRACE_CONFIRMED
    assert props[cs.TRACE_PROP_STATIC_MISSED] is False
    assert props[cs.TRACE_PROP_CALL_COUNT] == 3
    assert props[cs.TRACE_PROP_STALE] is False
    assert store.flushed == 1


def test_carry_keeps_a_pair_without_a_static_edge_runtime_only(tmp_path: Path) -> None:
    # The old edge's site is not carried: there is no source under tmp_path
    # to find the dispatch literal in, so the rewritten edge says so.
    store = _Store(
        captured=[_captured_row(line=9, col=2, dispatch_literal=True)],
        endpoints=_both_endpoints(),
    )

    carry_trace_edges(store, _capture(store), tmp_path)

    assert store.writes == []
    props = _written(store)
    assert props[cs.KEY_RESOLUTION] == cs.EdgeResolution.DYNAMIC
    assert props[cs.TRACE_PROP_STATIC_MISSED] is True
    assert props[cs.KEY_UNLOCATABLE] is True
    assert cs.KEY_LINE not in props
    assert cs.KEY_DISPATCH_LITERAL not in props


@pytest.mark.parametrize(
    ("caller_hash", "callee_hash"),
    [("ah1:caller-edited", "ah1:callee"), ("ah1:caller", "ah1:callee-edited")],
    ids=["caller-changed", "callee-changed"],
)
def test_carry_grades_an_edge_stale_when_an_endpoint_changed(
    tmp_path: Path, caller_hash: str, callee_hash: str
) -> None:
    store = _Store(
        captured=[_captured_row()],
        endpoints=_both_endpoints(caller_hash, callee_hash),
    )

    summary = carry_trace_edges(store, _capture(store), tmp_path)

    assert summary.newly_stale == 1
    assert _written(store)[cs.TRACE_PROP_STALE] is True


def test_carry_keeps_an_unchanged_edge_fresh(tmp_path: Path) -> None:
    store = _Store(captured=[_captured_row()], endpoints=_both_endpoints())

    summary = carry_trace_edges(store, _capture(store), tmp_path)

    assert summary.newly_stale == 0
    assert _written(store)[cs.TRACE_PROP_STALE] is False


def test_carry_keeps_a_stale_edge_stale_when_nothing_changed_since(
    tmp_path: Path,
) -> None:
    # An edit and its revert across two syncs must not make an observation
    # of the edited code look current again; only a new ingest does.
    store = _Store(
        captured=[_captured_row(dynamic_stale=True)], endpoints=_both_endpoints()
    )

    summary = carry_trace_edges(store, _capture(store), tmp_path)

    assert summary.newly_stale == 0
    assert _written(store)[cs.TRACE_PROP_STALE] is True


def test_carry_does_not_guess_stale_without_hashes(tmp_path: Path) -> None:
    # A Module endpoint has no anchor hash, nor does a graph written before
    # hashes existed: no evidence of a change, so no stale flag.
    store = _Store(
        captured=[_captured_row(from_hash=None)],
        endpoints=_both_endpoints(caller_hash="ah1:anything"),
    )

    carry_trace_edges(store, _capture(store), tmp_path)

    assert _written(store)[cs.TRACE_PROP_STALE] is False


def test_carry_drops_an_edge_whose_endpoint_no_longer_exists(tmp_path: Path) -> None:
    store = _Store(
        captured=[_captured_row()],
        endpoints=[_endpoint_row(_CALLER, "pkg/a.py", "ah1:caller")],
    )

    with patch("codebase_rag.trace.carry.logger") as log:
        summary = carry_trace_edges(store, _capture(store), tmp_path)

    assert (summary.carried, summary.dropped) == (0, 1)
    assert store.edges == []
    assert store.writes == []
    log.warning.assert_called_once_with(
        ls.TRACE_EDGES_OUTDATED, carried=0, stale=0, dropped=1
    )


def test_carry_drops_an_edge_into_a_deleted_file_whose_name_came_back(
    tmp_path: Path,
) -> None:
    # A same-stem survivor can take a deleted file's qualified names; the
    # observation was of the deleted definition and must not move onto it.
    store = _Store(captured=[_captured_row()], endpoints=_both_endpoints())

    summary = carry_trace_edges(
        store, _capture(store), tmp_path, gone_paths=frozenset({"pkg/b.py"})
    )

    assert (summary.carried, summary.dropped) == (0, 1)
    assert store.edges == []


def test_carry_with_nothing_captured_reads_and_writes_nothing(tmp_path: Path) -> None:
    store = _Store()

    summary = carry_trace_edges(store, [], tmp_path)

    assert (summary.carried, summary.newly_stale, summary.dropped) == (0, 0, 0)
    assert store.reads == []
    assert store.writes == []
    assert store.edges == []


def test_carry_logs_quietly_when_every_edge_is_current(tmp_path: Path) -> None:
    store = _Store(captured=[_captured_row()], endpoints=_both_endpoints())

    with patch("codebase_rag.trace.carry.logger") as log:
        carry_trace_edges(store, _capture(store), tmp_path)

    log.warning.assert_not_called()
    log.info.assert_called_once_with(ls.TRACE_EDGES_CARRIED, carried=1)


def _updater(store: _Store, tmp_path: Path, full_build: bool) -> GraphUpdater:
    updater = GraphUpdater(ingestor=store, repo_path=tmp_path, parsers={}, queries={})
    updater._is_full_build = full_build
    return updater


def test_a_capture_outage_aborts_an_incremental_sync(tmp_path: Path) -> None:
    updater = _updater(_Store(fail_reads=True), tmp_path, full_build=False)

    with pytest.raises(RuntimeError, match="store down"):
        updater._capture_trace_edges(["pkg/a.py"])


def test_a_capture_outage_on_a_full_build_warns_and_continues(tmp_path: Path) -> None:
    updater = _updater(_Store(fail_reads=True), tmp_path, full_build=True)

    with patch("codebase_rag.graph_updater.logger") as log:
        assert updater._capture_trace_edges(["pkg/a.py"]) == []

    log.warning.assert_called_once_with(ls.TRACE_CARRY_CAPTURE_FAILED)


def test_a_carry_failure_warns_instead_of_failing_the_sync(tmp_path: Path) -> None:
    captured = _capture(_Store(captured=[_captured_row()]))
    updater = _updater(_Store(fail_reads=True), tmp_path, full_build=False)

    with patch("codebase_rag.graph_updater.logger") as log:
        updater._carry_trace_edges(captured, ())

    log.warning.assert_called_once_with(
        ls.TRACE_CARRY_FAILED.format(count=1, error="store down")
    )
