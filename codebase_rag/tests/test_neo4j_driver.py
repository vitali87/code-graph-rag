"""The cursor-shaped adapter over the Neo4j driver (issue #1590).

`MemgraphIngestor` is written against a DB-API-ish connection/cursor, so
these tests pin the adapter to exactly that contract -- including the two
details the ingestor depends on that the Neo4j driver does not natively
provide: `cursor.description` names, and rows that survive the cursor
being closed.
"""

from __future__ import annotations

import pytest

from codebase_rag.services.neo4j_driver import (
    Neo4jConnection,
    Neo4jCursor,
)
from codebase_rag.types_defs import ConnectionProtocol, CursorProtocol


class FakeRecord:
    def __init__(self, values: tuple[object, ...]) -> None:
        self._values = values

    def values(self) -> tuple[object, ...]:
        return self._values


class FakeResult:
    def __init__(self, keys: list[str], rows: list[tuple[object, ...]]) -> None:
        self._keys, self._rows = keys, rows

    def keys(self) -> list[str]:
        return self._keys

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(FakeRecord(r) for r in self._rows)


class FakeSession:
    """Records what the adapter asks the driver to do."""

    def __init__(self, keys: list[str] | None = None, rows=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self._keys = keys or []
        self._rows = rows or []

    def run(self, query: str, parameters: dict) -> FakeResult:
        self.calls.append((query, parameters))
        return FakeResult(self._keys, self._rows)

    def close(self) -> None:
        self.closed = True


class TestCursorContract:
    def test_it_satisfies_the_cursor_protocol(self) -> None:
        assert isinstance(Neo4jCursor(FakeSession()), CursorProtocol)

    def test_description_exposes_column_names(self) -> None:
        # The ingestor builds result dicts from `desc.name`; the Neo4j
        # driver has no `description` at all, so the adapter supplies it.
        cur = Neo4jCursor(FakeSession(keys=["a", "b"], rows=[(1, 2)]))
        cur.execute("RETURN 1 AS a, 2 AS b")
        assert [d.name for d in (cur.description or [])] == ["a", "b"]

    def test_description_is_none_when_a_statement_returns_nothing(self) -> None:
        cur = Neo4jCursor(FakeSession(keys=[], rows=[]))
        cur.execute("CREATE INDEX foo IF NOT EXISTS FOR (n:L) ON (n.p)")
        assert cur.description is None

    def test_rows_are_buffered_at_execute_time(self) -> None:
        # A Neo4j Result is invalid once the session moves on, but the
        # ingestor closes its cursor in a `finally` and reads rows after.
        session = FakeSession(keys=["n"], rows=[(1,), (2,)])
        cur = Neo4jCursor(session)
        cur.execute("MATCH (n) RETURN n")
        session.run("SOMETHING ELSE", {})
        assert cur.fetchall() == [(1,), (2,)]

    def test_parameters_are_passed_through(self) -> None:
        session = FakeSession()
        Neo4jCursor(session).execute("MATCH (n {x: $x}) RETURN n", {"x": 1})
        assert session.calls == [("MATCH (n {x: $x}) RETURN n", {"x": 1})]

    def test_a_missing_parameter_map_becomes_an_empty_one(self) -> None:
        session = FakeSession()
        Neo4jCursor(session).execute("RETURN 1")
        assert session.calls == [("RETURN 1", {})]

    def test_a_batch_wrapper_is_passed_as_a_mapping(self) -> None:
        # BatchWrapper is a TypedDict, i.e. a plain dict at runtime; this
        # is the `UNWIND $batch AS row` path every bulk write uses.
        session = FakeSession()
        rows = [{"id": 1, "props": {}}]
        Neo4jCursor(session).execute("UNWIND $batch AS row", {"batch": rows})
        assert session.calls[0][1] == {"batch": rows}

    def test_a_bare_sequence_is_rejected(self) -> None:
        # A positional sequence has no name to bind to, so guessing would
        # silently send the wrong parameters.
        with pytest.raises(TypeError, match="must be a mapping"):
            Neo4jCursor(FakeSession()).execute("UNWIND $batch AS row", [{"id": 1}])

    def test_close_releases_buffered_rows(self) -> None:
        cur = Neo4jCursor(FakeSession(keys=["n"], rows=[(1,)]))
        cur.execute("MATCH (n) RETURN n")
        cur.close()
        assert cur.fetchall() == []


class TestConnectionContract:
    def test_it_satisfies_the_connection_protocol(self) -> None:
        assert isinstance(Neo4jConnection(FakeSession()), ConnectionProtocol)

    def test_it_hands_out_cursors(self) -> None:
        assert isinstance(Neo4jConnection(FakeSession()).cursor(), Neo4jCursor)

    def test_autocommit_is_accepted(self) -> None:
        # The ingestor sets `conn.autocommit = True`; Session.run is
        # already an auto-commit transaction, so this must not raise.
        conn = Neo4jConnection(FakeSession())
        conn.autocommit = True
        assert conn.autocommit is True

    def test_closing_closes_the_session(self) -> None:
        session = FakeSession()
        Neo4jConnection(session).close()
        assert session.closed

    def test_it_works_as_a_context_manager(self) -> None:
        session = FakeSession()
        with Neo4jConnection(session):
            assert not session.closed
        assert session.closed
