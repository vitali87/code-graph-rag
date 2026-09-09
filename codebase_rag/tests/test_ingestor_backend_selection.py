"""The ingestor's engine wiring (issue #1590).

The dialect object being correct is not enough: these tests assert the
statements that reach the connection, because the failure this guards
against is silent. `ensure_constraints` and `_ensure_indexes` wrap every
DDL statement in `except Exception: pass`, so an engine sent the wrong
syntax builds a graph with no constraints and no indexes rather than
raising -- and the parallel MERGE flush depends on those constraints.
"""

from __future__ import annotations

from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_NEO4J, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor, _apply_memory_limit


class RecordingCursor:
    def __init__(self, log: list[str], rows: list[tuple]) -> None:
        self._log, self._rows = log, rows

    def execute(self, query: str, params=None) -> None:
        self._log.append(query)

    @property
    def description(self):  # type: ignore[no-untyped-def]
        return None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def close(self) -> None:
        pass


class RecordingConn:
    """A connection that records statements without needing a server."""

    def __init__(self, rows: list[tuple] | None = None) -> None:
        self.log: list[str] = []
        self._rows = rows or []

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.log, self._rows)

    def close(self) -> None:
        pass


def _ingestor(dialect_name: str) -> tuple[MemgraphIngestor, RecordingConn]:
    ingestor = MemgraphIngestor(
        host="localhost", port=7687, dialect=get_dialect(dialect_name)
    )
    conn = RecordingConn()
    ingestor.conn = conn
    return ingestor, conn


class TestConstraintsReachTheConnection:
    def test_memgraph_emits_assert_form_constraints(self) -> None:
        ingestor, conn = _ingestor(DIALECT_MEMGRAPH)
        ingestor.ensure_constraints()
        assert any("ASSERT" in q and "IS UNIQUE" in q for q in conn.log)

    def test_neo4j_emits_require_form_constraints(self) -> None:
        ingestor, conn = _ingestor(DIALECT_NEO4J)
        ingestor.ensure_constraints()
        assert any("REQUIRE" in q and "IS UNIQUE" in q for q in conn.log)

    def test_neo4j_never_emits_memgraph_ddl(self) -> None:
        ingestor, conn = _ingestor(DIALECT_NEO4J)
        ingestor.ensure_constraints()
        assert not [q for q in conn.log if "ASSERT" in q]

    def test_memgraph_never_emits_neo4j_ddl(self) -> None:
        ingestor, conn = _ingestor(DIALECT_MEMGRAPH)
        ingestor.ensure_constraints()
        assert not [q for q in conn.log if "REQUIRE" in q]

    def test_neo4j_uses_the_plain_show_constraints_statement(self) -> None:
        ingestor, conn = _ingestor(DIALECT_NEO4J)
        ingestor.ensure_constraints()
        assert "SHOW CONSTRAINTS" in conn.log
        assert "SHOW CONSTRAINT INFO;" not in conn.log

    def test_memgraph_uses_the_info_form(self) -> None:
        ingestor, conn = _ingestor(DIALECT_MEMGRAPH)
        ingestor.ensure_constraints()
        assert "SHOW CONSTRAINT INFO;" in conn.log

    def test_both_engines_issue_the_same_number_of_statements(self) -> None:
        # The engines must cover the same schema; a shortfall on one side
        # would mean a label silently left unconstrained.
        memgraph, mg_conn = _ingestor(DIALECT_MEMGRAPH)
        memgraph.ensure_constraints()
        neo4j, neo_conn = _ingestor(DIALECT_NEO4J)
        neo4j.ensure_constraints()
        assert len(mg_conn.log) == len(neo_conn.log)

    def test_indexes_are_created_for_both_engines(self) -> None:
        for name in (DIALECT_MEMGRAPH, DIALECT_NEO4J):
            ingestor, conn = _ingestor(name)
            ingestor.ensure_constraints()
            assert [q for q in conn.log if "INDEX" in q], name


class TestMemoryLimit:
    def test_memgraph_reads_carry_the_limit(self) -> None:
        ingestor, conn = _ingestor(DIALECT_MEMGRAPH)
        ingestor.fetch_all("MATCH (n) RETURN n")
        assert "QUERY MEMORY LIMIT" in conn.log[0]

    def test_neo4j_reads_do_not(self) -> None:
        # Every read goes through fetch_all, so leaking this suffix would
        # make the entire read path a syntax error on Neo4j.
        ingestor, conn = _ingestor(DIALECT_NEO4J)
        ingestor.fetch_all("MATCH (n) RETURN n")
        assert "QUERY MEMORY LIMIT" not in conn.log[0]
        assert conn.log[0] == "MATCH (n) RETURN n"

    def test_the_module_helper_defaults_to_the_configured_engine(self) -> None:
        assert "QUERY MEMORY LIMIT" in _apply_memory_limit("MATCH (n) RETURN n", 512)

    def test_the_module_helper_honours_an_explicit_dialect(self) -> None:
        assert (
            _apply_memory_limit("MATCH (n) RETURN n", 512, get_dialect(DIALECT_NEO4J))
            == "MATCH (n) RETURN n"
        )


class TestDialectResolution:
    def test_the_default_engine_is_memgraph(self) -> None:
        # An existing install must keep working with no configuration.
        assert MemgraphIngestor(host="h", port=1)._dialect.name == DIALECT_MEMGRAPH

    def test_an_explicit_dialect_wins(self) -> None:
        ingestor = MemgraphIngestor(
            host="h", port=1, dialect=get_dialect(DIALECT_NEO4J)
        )
        assert ingestor._dialect.name == DIALECT_NEO4J

    def test_the_engine_is_fixed_at_construction(self, monkeypatch) -> None:
        # Re-reading settings per call would let one run straddle two
        # dialects if configuration changed underneath it.
        ingestor = MemgraphIngestor(host="h", port=1)
        monkeypatch.setattr(
            "codebase_rag.services.graph_service.settings.GRAPH_BACKEND",
            DIALECT_NEO4J,
        )
        assert ingestor._dialect.name == DIALECT_MEMGRAPH

    def test_writes_do_not_carry_a_memory_limit_on_either_engine(self) -> None:
        for name in (DIALECT_MEMGRAPH, DIALECT_NEO4J):
            ingestor, conn = _ingestor(name)
            ingestor.execute_write("CREATE (n:Thing)")
            assert "QUERY MEMORY LIMIT" not in conn.log[0], name


class TestNeo4jDriverIsNotRequiredForMemgraph:
    def test_memgraph_never_builds_a_neo4j_driver(self, monkeypatch) -> None:
        # The neo4j package is an optional extra; a Memgraph install must
        # not import it.
        def explode(*args: object, **kwargs: object) -> None:
            raise AssertionError("neo4j driver must not be constructed")

        monkeypatch.setattr(
            "codebase_rag.services.graph_service.MemgraphIngestor._neo4j_driver",
            explode,
        )
        ingestor, _ = _ingestor(DIALECT_MEMGRAPH)
        ingestor.ensure_constraints()


class TestNeo4jDriverLifecycle:
    """The pooled driver is shared state; these are its two failure modes."""

    def test_one_driver_is_shared_across_concurrent_creators(self, monkeypatch) -> None:
        # The parallel flush calls _create_connection from several worker
        # threads at once. An unguarded `if self._driver is None` builds a
        # second pool, which then leaks.
        import threading

        built: list[object] = []
        barrier = threading.Barrier(8)

        class FakeDriver:
            def __init__(self, **kwargs: object) -> None:
                built.append(self)

            def connect(self) -> RecordingConn:
                return RecordingConn()

            def close(self) -> None:
                pass

        monkeypatch.setattr(
            "codebase_rag.services.neo4j_driver.Neo4jDriver", FakeDriver
        )
        ingestor = MemgraphIngestor(
            host="h", port=1, dialect=get_dialect(DIALECT_NEO4J)
        )

        def race() -> None:
            barrier.wait()
            ingestor._neo4j_driver()

        threads = [threading.Thread(target=race) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(built) == 1

    def test_the_driver_is_closed_on_exit(self, monkeypatch) -> None:
        # __exit__ closes `self.conn`, which is a single session; without
        # this the pool behind it survives for the life of the process.
        closed: list[bool] = []

        class FakeDriver:
            def __init__(self, **kwargs: object) -> None:
                pass

            def connect(self) -> RecordingConn:
                return RecordingConn()

            def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(
            "codebase_rag.services.neo4j_driver.Neo4jDriver", FakeDriver
        )
        ingestor = MemgraphIngestor(
            host="h", port=1, dialect=get_dialect(DIALECT_NEO4J)
        )
        ingestor.__enter__()
        ingestor._neo4j_driver()
        ingestor.__exit__(None, None, None)

        assert closed == [True]

    def test_exiting_without_a_driver_does_not_raise(self) -> None:
        # A Memgraph run never builds one; the teardown must tolerate that.
        ingestor = MemgraphIngestor(host="h", port=1)
        ingestor.conn = RecordingConn()
        ingestor.__exit__(None, None, None)


class TestLegacyMigrationUsesTheDiscoveredName:
    """The DROP must name the constraint the server actually reported."""

    class _RowsConn(RecordingConn):
        """A connection whose SHOW CONSTRAINTS returns one legacy row."""

        def __init__(self, rows: list[tuple], columns: list[str]) -> None:
            super().__init__()
            self._rows, self._columns = rows, columns

        def cursor(self):  # type: ignore[no-untyped-def]
            conn = self

            class _Cursor:
                def execute(self, query: str, params=None) -> None:
                    conn.log.append(query)
                    self._is_show = "SHOW CONSTRAINT" in query

                @property
                def description(self):  # type: ignore[no-untyped-def]
                    if not getattr(self, "_is_show", False):
                        return None
                    return [type("C", (), {"name": c})() for c in conn._columns]

                def fetchall(self):  # type: ignore[no-untyped-def]
                    return conn._rows if getattr(self, "_is_show", False) else []

                def close(self) -> None:
                    pass

            return _Cursor()

    def test_neo4j_drops_the_name_the_server_reported(self) -> None:
        ingestor = MemgraphIngestor(
            host="h", port=1, dialect=get_dialect(DIALECT_NEO4J)
        )
        conn = self._RowsConn(
            rows=[("legacy_folder_path_uc", ["Folder"], ["path"])],
            columns=["name", "labelsOrTypes", "properties"],
        )
        ingestor.conn = conn
        ingestor.ensure_constraints()

        drops = [q for q in conn.log if q.startswith("DROP CONSTRAINT")]
        assert drops == ["DROP CONSTRAINT legacy_folder_path_uc IF EXISTS"]

    def test_memgraph_still_drops_by_pattern(self) -> None:
        ingestor = MemgraphIngestor(
            host="h", port=1, dialect=get_dialect(DIALECT_MEMGRAPH)
        )
        conn = self._RowsConn(
            rows=[("Folder", ["path"])], columns=["label", "properties"]
        )
        ingestor.conn = conn
        ingestor.ensure_constraints()

        drops = [q for q in conn.log if q.startswith("DROP CONSTRAINT")]
        assert drops == ["DROP CONSTRAINT ON (n:Folder) ASSERT n.path IS UNIQUE;"]
