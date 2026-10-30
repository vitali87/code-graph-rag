"""`GraphBatching`: the ingestor's write buffers and their flush.

Driven through a `GraphIngestor` writing to a real embedded graph, once on
the Rust extension's batch writes and once on the Cypher path, so both are
held to the same answers: when a buffer flushes, how a repeated node or
edge merges, which rows are dropped and how a lost row is reported. The
error handling of one batch statement is pinned on a scripted connection,
the only way to make a statement fail on cue.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.embedded_graph import native
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_SQLITE, get_dialect
from codebase_rag.services.graph_batching import GraphBatching
from codebase_rag.services.graph_service import (
    GraphConnection,
    GraphIngestor,
    WriteTuning,
)
from codebase_rag.types_defs import (
    BatchParams,
    BatchWrapper,
    PropertyValue,
    ResultValue,
)

_FN = cs.NodeLabel.FUNCTION.value
_QN = cs.KEY_QUALIFIED_NAME

type OpenGraph = Callable[..., AbstractContextManager[GraphIngestor]]


def _fn(qn: str) -> tuple[str, str, str]:
    return (_FN, _QN, qn)


@pytest.fixture
def messages() -> Iterator[list[str]]:
    seen: list[str] = []
    sink = logger.add(
        lambda m: seen.append(m.record["message"]), level="INFO", format="{message}"
    )
    yield seen
    logger.remove(sink)


@pytest.fixture(params=[True, False], ids=["rust_ingest", "cypher_ingest"])
def open_graph(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> OpenGraph:
    monkeypatch.setattr(native, "INGEST", request.param)

    @contextmanager
    def opened(
        batch_size: int = 1000, use_merge: bool = True
    ) -> Iterator[GraphIngestor]:
        with GraphIngestor(
            connection=GraphConnection(
                dialect=get_dialect(DIALECT_SQLITE),
                sqlite_path=str(tmp_path / "graph.sqlite"),
            ),
            tuning=WriteTuning(batch_size=batch_size, use_merge=use_merge),
        ) as ingestor:
            ingestor.ensure_constraints()
            yield ingestor

    return opened


def _functions(graph: GraphIngestor) -> dict[str, ResultValue]:
    rows = graph.fetch_all(
        "MATCH (n:Function) RETURN n.qualified_name AS qn, properties(n) AS props"
    )
    return {str(r["qn"]): r["props"] for r in rows}


def _edges(graph: GraphIngestor) -> list[tuple[str, str, str, ResultValue]]:
    rows = graph.fetch_all(
        "MATCH (a)-[r]->(b) RETURN a.qualified_name AS a, type(r) AS t, "
        "b.qualified_name AS b, properties(r) AS p"
    )
    return sorted(
        ((str(r["a"]), str(r["t"]), str(r["b"]), r["p"]) for r in rows),
        key=repr,
    )


class TestNodeBuffer:
    def test_nodes_wait_until_the_batch_fills(self, open_graph: OpenGraph) -> None:
        with open_graph(batch_size=3) as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            graph.ensure_node_batch(_FN, {_QN: "m.b"})

            assert _functions(graph) == {}
            assert len(graph.node_buffer) == 2

            graph.ensure_node_batch(_FN, {_QN: "m.c"})

            assert set(_functions(graph)) == {"m.a", "m.b", "m.c"}
            assert graph.node_buffer == []

    def test_a_repeated_node_merges_into_one_with_every_property(
        self, open_graph: OpenGraph
    ) -> None:
        with open_graph() as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a", "name": "a", "start_line": 1})
            graph.ensure_node_batch(_FN, {_QN: "m.a", "start_line": 4, "doc": "d"})
            graph.flush_all()

            assert _functions(graph) == {
                "m.a": {_QN: "m.a", "name": "a", "start_line": 4, "doc": "d"}
            }

    def test_a_node_with_no_key_is_dropped_and_its_label_still_written(
        self, open_graph: OpenGraph, messages: list[str]
    ) -> None:
        with open_graph() as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            graph.ensure_node_batch(_FN, {cs.KEY_NAME: "anonymous"})
            # A label with no unique key cannot be merged on anything.
            graph.ensure_node_batch("NoSuchLabel", {_QN: "m.x"})
            graph.flush_nodes()

            assert set(_functions(graph)) == {"m.a"}
            assert graph.fetch_all("MATCH (n:NoSuchLabel) RETURN n") == []
            # Dropped for good: a retry could not give them a key.
            assert graph.node_buffer == []
        assert "Flushed 1 of 3 buffered nodes." in messages
        assert any(m.startswith("Skipped 2 buffered nodes") for m in messages)


class TestRelationshipBuffer:
    def test_a_full_batch_writes_its_endpoints_first(
        self, open_graph: OpenGraph
    ) -> None:
        with open_graph(batch_size=4) as graph:
            for qn in ("m.a", "m.b", "m.c"):
                graph.ensure_node_batch(_FN, {_QN: qn})
            graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.b"))
            graph.ensure_relationship_batch(_fn("m.b"), "CALLS", _fn("m.c"))
            graph.ensure_relationship_batch(_fn("m.c"), "CALLS", _fn("m.a"))
            assert _edges(graph) == []

            graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.c"))

            assert {(a, t, b) for a, t, b, _p in _edges(graph)} == {
                ("m.a", "CALLS", "m.b"),
                ("m.b", "CALLS", "m.c"),
                ("m.c", "CALLS", "m.a"),
                ("m.a", "CALLS", "m.c"),
            }
            assert graph.node_buffer == []

    def test_each_call_site_keeps_its_own_edge(self, open_graph: OpenGraph) -> None:
        # One edge per site (issue #1522): the same site twice is one edge,
        # and a row naming no site merges onto the endpoints' existing edge.
        with open_graph() as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            graph.ensure_node_batch(_FN, {_QN: "m.b"})
            for site in ({"line": 1, "col": 2}, {"line": 3, "col": 4}):
                graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.b"), site)
            graph.ensure_relationship_batch(
                _fn("m.a"), "CALLS", _fn("m.b"), {"line": 1, "col": 2}
            )
            graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.b"))
            graph.flush_all()

            assert [p for _a, _t, _b, p in _edges(graph)] == [
                {"line": 1, "col": 2},
                {"line": 3, "col": 4},
            ]

    def test_an_edge_without_sites_merges_on_its_endpoints(
        self, open_graph: OpenGraph
    ) -> None:
        with open_graph() as graph:
            graph.ensure_node_batch(cs.NodeLabel.MODULE.value, {_QN: "m"})
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            for _ in range(2):
                graph.ensure_relationship_batch(
                    (cs.NodeLabel.MODULE.value, _QN, "m"), "DEFINES", _fn("m.a")
                )
            graph.flush_all()

            assert [(a, t, b) for a, t, b, _p in _edges(graph)] == [
                ("m", "DEFINES", "m.a")
            ]

    def test_create_mode_writes_every_row(self, open_graph: OpenGraph) -> None:
        with open_graph(use_merge=False) as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            graph.ensure_node_batch(_FN, {_QN: "m.b"})
            for _ in range(2):
                graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.b"))
            graph.flush_all()

            assert len(_edges(graph)) == 2

    def test_a_row_whose_endpoint_is_missing_is_counted_and_named(
        self, open_graph: OpenGraph, messages: list[str]
    ) -> None:
        with open_graph() as graph:
            graph.ensure_node_batch(_FN, {_QN: "m.a"})
            graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.gone"))
            graph.ensure_relationship_batch(_fn("m.ghost"), "CALLS", _fn("m.a"))
            graph.ensure_relationship_batch(_fn("m.a"), "CALLS", _fn("m.a"))
            graph.flush_all()

            assert [(a, b) for a, _t, b, _p in _edges(graph)] == [("m.a", "m.a")]
        # Numbered in the order the lookup returns them, which is unspecified.
        failed = {m.split(": ", 1)[1] for m in messages if m.startswith("  Failed ")}
        assert failed == {
            "Function.m.a -> Function.m.gone (missing target)",
            "Function.m.ghost -> Function.m.a (missing source)",
        }
        assert "Flushed 3 relationships (1 successful, 2 failed)." in messages


class _Cursor:
    def __init__(self, fail_with: Exception | None) -> None:
        self.fail_with = fail_with
        self.executed: list[tuple[str, Sequence[BatchParams]]] = []
        self.closed = False
        self.description = None

    def execute(self, query: str, params: BatchWrapper) -> None:
        self.executed.append((query, params["batch"]))
        if self.fail_with is not None:
            raise self.fail_with

    def fetchall(self) -> list[tuple[PropertyValue, ...]]:
        return []

    def close(self) -> None:
        self.closed = True


class _Connection:
    """A Memgraph connection whose every statement fails with `fail_with`."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.cursors: list[_Cursor] = []

    def cursor(self) -> _Cursor:
        self.cursors.append(_Cursor(self.fail_with))
        return self.cursors[-1]

    def close(self) -> None:
        return None


def _scripted(conn: _Connection) -> GraphBatching:
    # Typed as the mixin: these tests reach nothing but its buffers.
    ingestor = GraphIngestor(
        host="localhost",
        port=7687,
        connection=GraphConnection(dialect=get_dialect(DIALECT_MEMGRAPH)),
    )
    ingestor.conn = conn
    return ingestor


class TestBatchStatement:
    def test_nothing_buffered_opens_no_cursor(self) -> None:
        conn = _Connection()

        _scripted(conn).flush_all()

        assert conn.cursors == []

    def test_a_failed_statement_is_raised_logged_and_its_cursor_closed(
        self, messages: list[str]
    ) -> None:
        conn = _Connection(RuntimeError("disk full"))
        ingestor = _scripted(conn)
        for index in range(12):
            ingestor.ensure_node_batch(_FN, {_QN: f"m.f{index}"})

        with pytest.raises(RuntimeError, match="disk full"):
            ingestor.flush_nodes()

        assert [c.closed for c in conn.cursors] == [True]
        assert "!!! Batch Cypher Error: disk full" in messages
        assert any(m.startswith("    Query: ") for m in messages)
        # A long batch is logged by its head only.
        assert any(m.startswith("    Params (first 10 of 12): ") for m in messages)
        # The rows stay buffered for the retry.
        assert len(ingestor.node_buffer) == 12

    def test_an_already_exists_refusal_is_raised_without_an_error_log(
        self, messages: list[str]
    ) -> None:
        conn = _Connection(RuntimeError("Constraint ALREADY EXISTS"))
        ingestor = _scripted(conn)
        ingestor.ensure_node_batch(_FN, {_QN: "m.a"})

        with pytest.raises(RuntimeError):
            ingestor.flush_nodes()

        assert not any(m.startswith("!!! Batch Cypher Error") for m in messages)
        assert [c.closed for c in conn.cursors] == [True]
