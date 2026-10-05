from __future__ import annotations

import mgclient  # ty: ignore[unresolved-import]
import pytest

from codebase_rag.cypher_queries import CYPHER_AUDIT_ORPHANS
from codebase_rag.tools.health_checker import HealthChecker


def test_check_memgraph_connection_returns_failure_when_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_operational_error(**_: object) -> object:
        raise mgclient.OperationalError("connection refused")

    monkeypatch.setattr(mgclient, "connect", raise_operational_error)

    result = HealthChecker().check_memgraph_connection()

    assert result.passed is False


def test_check_memgraph_connection_logs_in_with_configured_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without the login, a Memgraph with a user refuses the probe and `cgr
    # health` reports a healthy authenticated server as down.
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "MEMGRAPH_USERNAME", "cgr")
    monkeypatch.setattr(settings, "MEMGRAPH_PASSWORD", "s3cret")
    logins: list[tuple[str | None, str | None]] = []

    def record_connect(
        host: str, port: int, username: str | None = None, password: str | None = None
    ) -> _FakeConnection:
        logins.append((username, password))
        return _FakeConnection(_FakeCursor({}, []))

    monkeypatch.setattr(mgclient, "connect", record_connect)

    result = HealthChecker().check_memgraph_connection()

    assert result.passed is True
    assert logins == [("cgr", "s3cret")]


class _FakeColumn:
    # Mirrors mgclient.Column: exposes .name and is NOT subscriptable.
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class _FakeCursor:
    def __init__(self, rows_by_marker: dict[str, list[tuple]], columns: list[str]):
        self._rows_by_marker = rows_by_marker
        self._columns = columns
        self._rows: list[tuple] = []
        self.closed = False

    def execute(self, query: str) -> None:
        self._rows = []
        for marker, rows in self._rows_by_marker.items():
            if marker in query:
                self._rows = rows
                return

    @property
    def description(self) -> list[_FakeColumn]:
        return [_FakeColumn(name) for name in self._columns]

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def test_check_graph_integrity_skipped_when_memgraph_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_operational_error(**_: object) -> object:
        raise mgclient.OperationalError("connection refused")

    monkeypatch.setattr(mgclient, "connect", raise_operational_error)

    assert HealthChecker().check_graph_integrity() == []


def test_check_graph_integrity_passes_on_clean_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor({}, [])
    monkeypatch.setattr(mgclient, "connect", lambda **_: _FakeConnection(cursor))

    results = HealthChecker().check_graph_integrity()

    assert len(results) == 1
    assert results[0].passed is True


def test_check_graph_integrity_reports_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(
        {CYPHER_AUDIT_ORPHANS: [("Method", 427)]}, ["label", "orphans"]
    )
    monkeypatch.setattr(mgclient, "connect", lambda **_: _FakeConnection(cursor))

    results = HealthChecker().check_graph_integrity()

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].error is not None
    assert "427" in results[0].error
