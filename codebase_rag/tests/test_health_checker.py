from __future__ import annotations

import mgclient  # ty: ignore[unresolved-import]
import pytest

from codebase_rag.cypher_queries import CYPHER_AUDIT_ORPHANS
from codebase_rag.tools.health_checker import HealthChecker


def test_check_graph_connection_returns_failure_when_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_operational_error(**_: object) -> object:
        raise mgclient.OperationalError("connection refused")

    # health_checker.py no longer imports mgclient directly; it reaches the
    # graph through get_ingestor(), which for the default Memgraph backend
    # connects via codebase_rag.services.graph.memgraph's mgclient reference.
    monkeypatch.setattr(
        "codebase_rag.services.graph.memgraph.mgclient.connect",
        raise_operational_error,
    )

    result = HealthChecker().check_graph_connection()

    assert result.passed is False


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

    def execute(self, query: str, params: dict[str, object] | None = None) -> None:
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
        self.autocommit = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


def _patch_mgclient_connect(monkeypatch: pytest.MonkeyPatch, connect: object) -> None:
    monkeypatch.setattr(
        "codebase_rag.services.graph.memgraph.mgclient.connect", connect
    )


def test_check_graph_integrity_skipped_when_graph_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_operational_error(**_: object) -> object:
        raise mgclient.OperationalError("connection refused")

    _patch_mgclient_connect(monkeypatch, raise_operational_error)

    assert HealthChecker().check_graph_integrity() == []


def test_check_graph_integrity_passes_on_clean_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor({}, [])
    _patch_mgclient_connect(monkeypatch, lambda **_: _FakeConnection(cursor))

    results = HealthChecker().check_graph_integrity()

    assert len(results) == 1
    assert results[0].passed is True


def test_check_graph_integrity_reports_orphans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(
        {CYPHER_AUDIT_ORPHANS: [("Method", 427)]}, ["label", "orphans"]
    )
    _patch_mgclient_connect(monkeypatch, lambda **_: _FakeConnection(cursor))

    results = HealthChecker().check_graph_integrity()

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].error is not None and "427" in results[0].error
