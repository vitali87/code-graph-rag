"""A Memgraph server older than cgr supports is named, not met query by query.

The MCP setup guide started `memgraph/memgraph-platform`, a discontinued
image shipping Memgraph 2.14.1, whose parser rejects the label alternation
(`MATCH (n:Class|Function)`) most read paths use. The sync logged a Cypher
error and reported success, every analysis then failed with "mismatched
input '|'", and `cgr doctor` passed (issue #2906).
"""

from __future__ import annotations

from pathlib import Path

import mgclient
import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services import graph_service
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tools.health_checker import HealthChecker
from codebase_rag.types_defs import PropertyDict, ResultValue

_DOCS = Path(__file__).resolve().parents[2] / "docs"


class _Cursor:
    def __init__(self, version: ResultValue) -> None:
        self._version = version
        self._rows: list[tuple[ResultValue, ...]] = []

    def execute(self, query: str, params: PropertyDict | None = None) -> None:
        self._rows = [(self._version,)] if "VERSION" in query else [(1,)]

    def fetchall(self) -> list[tuple[ResultValue, ...]]:
        return self._rows

    def close(self) -> None:
        return None


class _Connection:
    def __init__(self, version: ResultValue) -> None:
        self.autocommit = False
        self._version = version

    def cursor(self) -> _Cursor:
        return _Cursor(self._version)

    def close(self) -> None:
        return None


def _serve(monkeypatch: pytest.MonkeyPatch, version: ResultValue) -> None:
    monkeypatch.setattr(mgclient, "connect", lambda **_: _Connection(version))
    monkeypatch.setattr(graph_service, "_warned_versions", set())


def _enter(monkeypatch: pytest.MonkeyPatch, version: ResultValue) -> list[str]:
    _serve(monkeypatch, version)
    warnings: list[str] = []
    handler = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        with MemgraphIngestor(host="127.0.0.1", port=7999):
            pass
    finally:
        logger.remove(handler)
    return [w for w in warnings if "not supported" in w]


def test_doctor_fails_on_an_unsupported_memgraph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "2.14.1")
    result = HealthChecker().check_memgraph_connection()
    assert result.passed is False, result
    assert "2.14.1" in result.name, result
    assert result.error is not None and "cgr daemon up" in result.error, result


@pytest.mark.parametrize("version", ["3.3.0", "3.0.0", "4.1.2"])
def test_doctor_passes_a_supported_memgraph(
    monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    _serve(monkeypatch, version)
    assert HealthChecker().check_memgraph_connection().passed is True


def test_connecting_to_an_unsupported_memgraph_warns_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings = _enter(monkeypatch, "2.14.1")
    assert len(warnings) == 1, warnings
    assert "2.14.1" in warnings[0] and "cgr daemon up" in warnings[0], warnings
    # A second connection in the same process does not repeat it.
    handler_warnings: list[str] = []
    handler = logger.add(lambda m: handler_warnings.append(str(m)), level="WARNING")
    try:
        with MemgraphIngestor(host="127.0.0.1", port=7999):
            pass
    finally:
        logger.remove(handler)
    assert not [w for w in handler_warnings if "not supported" in w]


@pytest.mark.parametrize("version", ["3.3.0", None, "", "nightly", 3])
def test_a_supported_or_unreadable_version_warns_nothing(
    monkeypatch: pytest.MonkeyPatch, version: ResultValue
) -> None:
    # Negative: only a version read as below 3.x is reported.
    assert _enter(monkeypatch, version) == []


def test_the_mcp_setup_guide_starts_the_supported_server() -> None:
    text = (_DOCS / "claude-code-setup.md").read_text(encoding="utf-8")
    assert "memgraph-platform" not in text
    assert "cgr daemon up" in text


def test_the_version_floor_is_3() -> None:
    assert cs.MEMGRAPH_MIN_MAJOR_VERSION == 3
