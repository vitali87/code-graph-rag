"""`cgr health` must describe the engine it actually connected to.

Both failures here are silent: a wrong endpoint in the success message
reads as a healthy Memgraph while the tool is talking to Neo4j, and a
leaked driver shows up only as connection exhaustion much later.
"""

from __future__ import annotations

import pytest

from codebase_rag.graph_dialects import (
    DIALECT_MEMGRAPH,
    DIALECT_NEO4J,
    get_dialect,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tools.health_checker import (
    HealthChecker,
    _backend_connection,
    _backend_endpoint,
)


class FakeCursor:
    def execute(self, query: str, params: object = None) -> None:
        pass

    @property
    def description(self) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def close(self) -> None:
        pass


class FakeConn:
    def __init__(self) -> None:
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor()

    def close(self) -> None:
        self.closed = True


class TestReportedEndpoint:
    def test_memgraph_reports_its_host_and_port(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.GRAPH_BACKEND",
            DIALECT_MEMGRAPH,
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.MEMGRAPH_HOST", "mg-host"
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.MEMGRAPH_PORT", 7687
        )
        assert _backend_endpoint() == "mg-host:7687"

    def test_neo4j_reports_its_uri(self, monkeypatch) -> None:
        # The regression: reporting MEMGRAPH_HOST here names a server the
        # Neo4j install never talks to, while claiming it is responsive.
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.GRAPH_BACKEND", DIALECT_NEO4J
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.NEO4J_URI",
            "bolt://neo-server.example:7999",
        )
        assert _backend_endpoint() == "bolt://neo-server.example:7999"

    def test_the_success_message_carries_the_neo4j_endpoint(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.GRAPH_BACKEND", DIALECT_NEO4J
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.NEO4J_URI",
            "bolt://neo-server.example:7999",
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.MEMGRAPH_HOST", "mg-host"
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor._create_connection",
            lambda self: FakeConn(),
        )
        result = HealthChecker().check_memgraph_connection()
        assert result.passed
        assert "neo-server.example:7999" in result.message
        assert "mg-host" not in result.message


class TestReportedEngineName:
    """`cgr doctor` renders `result.name`, so the label names the service."""

    def _probe(self, monkeypatch, backend: str, uri: str = ""):
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.settings.GRAPH_BACKEND", backend
        )
        monkeypatch.setattr("codebase_rag.tools.health_checker.settings.NEO4J_URI", uri)
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor._create_connection",
            lambda self: FakeConn(),
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor.close_driver",
            lambda self: None,
        )
        return HealthChecker().check_memgraph_connection()

    def test_memgraph_keeps_its_label(self, monkeypatch) -> None:
        assert self._probe(monkeypatch, DIALECT_MEMGRAPH).name == (
            "Memgraph connection successful"
        )

    def test_neo4j_is_labelled_neo4j(self, monkeypatch) -> None:
        # The regression: a Neo4j deployment shown a "Memgraph connection"
        # check is sent looking for a server it does not run.
        result = self._probe(monkeypatch, DIALECT_NEO4J, "bolt://neo:7999")
        assert result.name == "Neo4j connection successful"
        assert "Memgraph" not in result.name


class TestCredentialValidationIsPerEngine:
    def test_neo4j_ignores_a_half_set_memgraph_credential(self) -> None:
        # Neo4j authenticates with NEO4J_USERNAME/NEO4J_PASSWORD, so a
        # leftover MEMGRAPH_USERNAME must not block startup.
        MemgraphIngestor(
            host="h",
            port=7687,
            username="legacy",
            password=None,
            dialect=get_dialect(DIALECT_NEO4J),
        )

    def test_memgraph_still_rejects_a_half_set_credential(self) -> None:
        with pytest.raises(ValueError, match="(?i)both"):
            MemgraphIngestor(
                host="h",
                port=7687,
                username="u",
                password=None,
                dialect=get_dialect(DIALECT_MEMGRAPH),
            )


class TestDriverIsReleased:
    def test_the_driver_is_closed_when_the_context_exits(self, monkeypatch) -> None:
        # Each health check builds its own ingestor, so without this every
        # call leaks a whole Neo4j connection pool.
        closed: list[str] = []
        conn = FakeConn()

        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor._create_connection",
            lambda self: conn,
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor.close_driver",
            lambda self: closed.append("driver"),
        )

        with _backend_connection() as handed_out:
            assert handed_out is conn

        assert conn.closed
        assert closed == ["driver"]

    def test_the_driver_is_closed_even_when_the_body_raises(self, monkeypatch) -> None:
        closed: list[str] = []
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor._create_connection",
            lambda self: FakeConn(),
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor.close_driver",
            lambda self: closed.append("driver"),
        )

        connection = _backend_connection()
        with pytest.raises(RuntimeError), connection:
            raise RuntimeError("boom")

        assert closed == ["driver"]

    def test_the_integrity_audit_releases_its_driver(self, monkeypatch) -> None:
        closed: list[str] = []
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor._create_connection",
            lambda self: FakeConn(),
        )
        monkeypatch.setattr(
            "codebase_rag.tools.health_checker.MemgraphIngestor.close_driver",
            lambda self: closed.append("driver"),
        )
        HealthChecker().check_graph_integrity()
        assert closed == ["driver"]

    def test_the_integrity_audit_releases_explicitly_not_via_gc(self) -> None:
        """The release must not depend on the garbage collector.

        Dropping the last reference to an abandoned generator-based
        context manager runs its `finally` anyway under CPython's
        refcounting, so asserting that `close_driver` was called cannot
        tell an explicit release from an accidental one -- a mutation
        removing the release passes. On a non-refcounting runtime (PyPy)
        the same code leaks a connection pool per health check until a
        collection happens. Assert the call exists in the source instead.
        """
        import inspect

        from codebase_rag.tools import health_checker

        source = inspect.getsource(health_checker.HealthChecker.check_graph_integrity)
        assert "connection.__exit__(None, None, None)" in source
