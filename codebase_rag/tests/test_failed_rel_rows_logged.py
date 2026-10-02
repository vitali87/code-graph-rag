"""Issue #2438: a relationship flush that loses rows names those rows.

The write (`MATCH (a), (b) MERGE (a)-[r]->(b) RETURN count(r)`) only says how
many rows it wrote. The warning used to print the first three rows of the batch
as "samples" whatever their outcome, so on a real repo it nearly always named
edges that had been created, and only CALLS was itemised at all.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import NamedTuple

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import BatchWrapper, ResultValue

CALLER = "proj.app.caller"
PHANTOM = "builtin.Error.prototype.customDelay"


class _Column(NamedTuple):
    name: str


class _FakeGraph:
    """A connection over a fixed node set, answering the flush's two queries.

    The write reports how many rows had both endpoints; any other query is the
    endpoint lookup, answered with the rows that had one missing.
    """

    def __init__(self, nodes: set[str], lookup_error: Exception | None = None) -> None:
        self.nodes = nodes
        self.lookup_error = lookup_error
        self.queries: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def close(self) -> None:
        return None


class _FakeCursor:
    def __init__(self, graph: _FakeGraph) -> None:
        self._graph = graph
        self._columns: list[_Column] = []
        self._rows: list[tuple[ResultValue, ...]] = []

    def execute(self, query: str, params: BatchWrapper | None = None) -> None:
        self._graph.queries.append(query)
        batch = params["batch"] if params else []
        nodes = self._graph.nodes
        if "MERGE" in query or "CREATE" in query:
            written = [
                r for r in batch if r["from_val"] in nodes and r["to_val"] in nodes
            ]
            self._columns = [_Column(cs.KEY_CREATED)]
            self._rows = [(len(written),)]
            return
        if self._graph.lookup_error is not None:
            raise self._graph.lookup_error
        self._columns = [
            _Column(cs.KEY_FROM_VAL),
            _Column(cs.KEY_TO_VAL),
            _Column(cs.KEY_FROM_MISSING),
            _Column(cs.KEY_TO_MISSING),
        ]
        self._rows = [
            (
                r["from_val"],
                r["to_val"],
                r["from_val"] not in nodes,
                r["to_val"] not in nodes,
            )
            for r in batch
            if r["from_val"] not in nodes or r["to_val"] not in nodes
        ]

    @property
    def description(self) -> list[_Column]:
        return self._columns

    def fetchall(self) -> list[tuple[ResultValue, ...]]:
        return self._rows

    def close(self) -> None:
        return None


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    logger.remove(sink)


def _flush(
    graph: _FakeGraph,
    rows: Sequence[tuple[str, str]],
    rel_type: str = cs.RelationshipType.CALLS,
    from_label: str = cs.NodeLabel.FUNCTION,
    to_label: str = cs.NodeLabel.FUNCTION,
) -> None:
    ingestor = MemgraphIngestor(host="localhost", port=7687, batch_size=10_000)
    ingestor.conn = graph
    for source, target in rows:
        ingestor.ensure_relationship_batch(
            (from_label, cs.KEY_QUALIFIED_NAME, source),
            rel_type,
            (to_label, cs.KEY_QUALIFIED_NAME, target),
        )
    ingestor.flush_relationships()


def _row_lines(warnings: list[str]) -> list[str]:
    # The lines under the header: one per named row, then the overflow count.
    return [w for w in warnings if w.startswith(("  Failed ", "  ... "))]


def test_the_warning_names_the_failed_row_not_the_head_of_the_batch(
    warnings: list[str],
) -> None:
    # The issue's shape: the failing row comes after rows that were written.
    written = [f"proj.app.ok_{i}" for i in range(4)]
    graph = _FakeGraph({CALLER, *written})

    _flush(graph, [*((CALLER, t) for t in written), (CALLER, PHANTOM)])

    rows = _row_lines(warnings)
    assert len(rows) == 1, warnings
    assert PHANTOM in rows[0], warnings
    assert not [w for w in warnings if "ok_" in w], warnings


def test_a_mixed_batch_lists_exactly_its_failures(warnings: list[str]) -> None:
    graph = _FakeGraph({CALLER, "proj.app.ok_1", "proj.app.ok_2", "proj.app.ok_3"})

    _flush(
        graph,
        [
            (CALLER, "proj.app.ok_1"),
            (CALLER, "proj.app.gone_1"),
            (CALLER, "proj.app.ok_2"),
            ("proj.app.ghost", "proj.app.ok_3"),
            (CALLER, "proj.app.gone_2"),
        ],
    )

    header = [w for w in warnings if w.startswith("Failed to create")]
    assert len(header) == 1, warnings
    assert "3 of 5" in header[0], warnings
    rows = _row_lines(warnings)
    assert len(rows) == 3, warnings
    assert "proj.app.gone_1" in rows[0], rows
    assert "missing target" in rows[0], rows
    assert "proj.app.ghost" in rows[1], rows
    assert "missing source" in rows[1], rows
    assert "proj.app.gone_2" in rows[2], rows
    assert "missing target" in rows[2], rows
    # Negative: a row that was written is never listed as a failure.
    assert not [r for r in rows if "ok_1" in r or "ok_2" in r], rows


def test_a_lost_row_of_another_relationship_type_is_named(
    warnings: list[str],
) -> None:
    # Not only CALLS: an IMPORTS row whose target module is missing is lost the
    # same way, and used to show up only as a count in the INFO summary.
    graph = _FakeGraph({"proj.a", "proj.b"})

    _flush(
        graph,
        [("proj.a", "proj.b"), ("proj.a", "proj.missing")],
        rel_type=cs.RelationshipType.IMPORTS,
        from_label=cs.NodeLabel.MODULE,
        to_label=cs.NodeLabel.MODULE,
    )

    assert any(cs.RelationshipType.IMPORTS in w for w in warnings), warnings
    rows = _row_lines(warnings)
    assert len(rows) == 1, warnings
    assert "proj.missing" in rows[0], warnings


def test_a_batch_that_wrote_every_row_logs_nothing(warnings: list[str]) -> None:
    # Negative: no failure, no warning, and no extra lookup query.
    graph = _FakeGraph({CALLER, "proj.app.ok_1", "proj.app.ok_2"})

    _flush(graph, [(CALLER, "proj.app.ok_1"), (CALLER, "proj.app.ok_2")])

    assert warnings == []
    assert len(graph.queries) == 1, graph.queries


def test_many_failures_are_listed_up_to_the_cap(warnings: list[str]) -> None:
    lost = [f"proj.app.gone_{i:02d}" for i in range(cs.FAILED_REL_ROWS_SHOWN + 5)]
    graph = _FakeGraph({CALLER})

    _flush(graph, [(CALLER, t) for t in lost])

    rows = _row_lines(warnings)
    assert len(rows) == cs.FAILED_REL_ROWS_SHOWN + 1, warnings
    assert "5 more" in rows[-1], rows


def test_a_failing_lookup_still_reports_the_count(warnings: list[str]) -> None:
    # Negative: the lookup is diagnostic only; when it fails the flush still
    # completes and the count of lost rows is still reported.
    graph = _FakeGraph({CALLER, "proj.app.ok_1"}, lookup_error=RuntimeError("down"))

    _flush(graph, [(CALLER, "proj.app.ok_1"), (CALLER, PHANTOM)])

    assert any("Failed to create 1 of 2" in w for w in warnings), warnings
    assert _row_lines(warnings) == [], warnings
