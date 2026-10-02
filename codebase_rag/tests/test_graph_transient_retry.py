"""Issue #2441: a write Memgraph rejects as a transient conflict is retried.

"Cannot resolve conflicting transactions. Retry this transaction when the
conflicting transaction is finished" ended the run with an ERROR, a
best-effort flush and a traceback. The ingestor autocommits, so a statement
the database rejected changed nothing, and re-sending it after a short wait
is what the message asks for. Anything else still fails at once.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence

import mgclient
import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyValue

_CONFLICT = (
    "Cannot resolve conflicting transactions. Retry this transaction when the "
    "conflicting transaction is finished."
)


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn
        self.description = None

    def execute(self, query: str, params: dict[str, PropertyValue]) -> None:
        self._conn.executed.append(query)
        if self._conn.failures:
            raise self._conn.failures.pop(0)

    def fetchall(self) -> list[Sequence[PropertyValue]]:
        return []

    def close(self) -> None:
        pass


class _Conn:
    autocommit = True

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = failures
        self.executed: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    return waits


@pytest.fixture
def errors() -> Iterator[list[str]]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="ERROR", format="{message}")
    yield messages
    logger.remove(sink)


def _ingestor(conn: _Conn) -> MemgraphIngestor:
    ingestor = MemgraphIngestor(host="127.0.0.1", port=7687)
    ingestor.conn = conn
    return ingestor


def _conflicts(count: int) -> list[Exception]:
    return [mgclient.TransientError(_CONFLICT) for _ in range(count)]


def test_a_conflicting_write_is_retried_until_it_goes_through(
    no_wait: list[float], errors: list[str]
) -> None:
    conn = _Conn(_conflicts(2))

    _ingestor(conn).execute_write("MATCH (m:Module) SET m.x = 1")

    assert len(conn.executed) == 3
    assert len(no_wait) == 2
    assert no_wait[1] > no_wait[0]
    assert errors == []


def test_a_conflicting_batch_is_retried(errors: list[str]) -> None:
    conn = _Conn(_conflicts(1))

    _ingestor(conn)._execute_batch_on(
        conn, "MERGE (n:Module {qualified_name: row.qn})", [{"qn": "click.core"}]
    )

    assert len(conn.executed) == 2
    assert errors == []


def test_a_conflict_that_outlasts_the_retries_is_raised(no_wait: list[float]) -> None:
    # Negative: bounded, and the last error is the one the caller sees.
    conn = _Conn(_conflicts(cs.MG_TRANSIENT_RETRY_ATTEMPTS))
    ingestor = _ingestor(conn)

    with pytest.raises(mgclient.TransientError):
        ingestor.execute_write("MATCH (m:Module) SET m.x = 1")

    assert len(conn.executed) == cs.MG_TRANSIENT_RETRY_ATTEMPTS
    assert len(no_wait) == cs.MG_TRANSIENT_RETRY_ATTEMPTS - 1


@pytest.mark.parametrize(
    "failure",
    [
        mgclient.DatabaseError("Invalid input 'MATC'"),
        mgclient.OperationalError("connection lost"),
    ],
    ids=["syntax", "connection"],
)
def test_other_errors_are_not_retried(failure: Exception, no_wait: list[float]) -> None:
    # Negative: only the error Memgraph says to retry is retried.
    conn = _Conn([failure])
    ingestor = _ingestor(conn)

    with pytest.raises(type(failure)):
        ingestor.execute_write("MATC (m) RETURN m")

    assert len(conn.executed) == 1
    assert no_wait == []
