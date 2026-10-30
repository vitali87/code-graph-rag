"""The SQLite file behind the embedded graph, opened without the engine.

`EmbeddedConnection` builds on `open_connection`; these tests drive the store
module on raw `sqlite3` connections, so what a fresh, reopened, read-only or
schema-1 file looks like after it is opened is pinned where it is decided.
The process-wide `hard_heap_limit` is left alone: SQLite only lets it be
lowered, so setting it here would cap every later test in the worker.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from codebase_rag.embedded_graph import constants as ecs
from codebase_rag.embedded_graph.store import (
    EmbeddedGraphUnsupported,
    check_sqlite,
    open_connection,
    pin_planner_statistics,
    stop_node_id_reuse,
    supports_json_arrow,
)

_SCHEMA_1_NODES = (
    "CREATE TABLE cgr_nodes(id INTEGER PRIMARY KEY, label TEXT NOT NULL, "
    "key TEXT, props TEXT NOT NULL DEFAULT '{}');"
    "CREATE UNIQUE INDEX cgr_nodes_label_key ON cgr_nodes(label, key) "
    "WHERE key IS NOT NULL;"
    "INSERT INTO cgr_nodes(id, label, key, props) VALUES "
    "(4, 'Function', 'm.a', '{\"name\": \"a\"}'), "
    "(9, 'Function', 'm.b', '{\"name\": \"b\"}');"
)


@pytest.fixture
def raw(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(tmp_path / "raw.sqlite", isolation_level=None)
    yield conn
    conn.close()


def _nodes_ddl(conn: sqlite3.Connection) -> str:
    (ddl,) = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'cgr_nodes'"
    ).fetchone()
    return str(ddl)


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT name, value FROM cgr_meta"))


def _stats(conn: sqlite3.Connection) -> set[tuple[str, str, str]]:
    return set(conn.execute("SELECT tbl, idx, stat FROM sqlite_stat1"))


class TestCheckSqlite:
    @pytest.mark.parametrize("version", [(3, 30, 0), (3, 45, 1)])
    def test_3_30_with_json_passes(
        self, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
    ) -> None:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
        check_sqlite()

    def test_an_older_sqlite_is_refused_with_both_versions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 29, 9))
        with pytest.raises(EmbeddedGraphUnsupported) as raised:
            check_sqlite()
        assert "3.30.0" in str(raised.value)
        assert sqlite3.sqlite_version in str(raised.value)

    def test_a_sqlite_without_json_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A build without the JSON functions fails the probe with "no such
        # function", which a call to one that never exists reproduces.
        monkeypatch.setattr(ecs, "EG_SQL_JSON_PROBE", "SELECT cgr_no_json('[]')")
        with pytest.raises(EmbeddedGraphUnsupported) as raised:
            check_sqlite()
        assert isinstance(raised.value.__cause__, sqlite3.OperationalError)

    @pytest.mark.parametrize(
        ("version", "expected"),
        [((3, 37, 9), False), ((3, 38, 0), True), ((3, 45, 1), True)],
    )
    def test_json_arrows_need_3_38(
        self,
        monkeypatch: pytest.MonkeyPatch,
        version: tuple[int, int, int],
        expected: bool,
    ) -> None:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", version)
        assert supports_json_arrow() is expected


class TestOpenConnection:
    def test_a_new_file_gets_the_schema_its_meta_and_the_pinned_statistics(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "nested" / "dirs" / "graph.sqlite"

        conn = open_connection(str(path))
        try:
            assert path.is_file()
            tables = {
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {"cgr_nodes", "cgr_edges", "cgr_meta"} <= tables
            assert ecs.EG_AUTOINCREMENT in _nodes_ddl(conn).upper()
            assert _meta(conn) == {
                ecs.EG_META_SCHEMA_VERSION: ecs.EG_SCHEMA_VERSION,
                ecs.EG_META_GRAPH_VERSION: "0",
            }
            assert _stats(conn) >= set(ecs.EG_PLANNER_STATISTICS)
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            # Autocommit: the executor opens its own IMMEDIATE transactions.
            assert conn.isolation_level is None
            assert not conn.in_transaction
        finally:
            conn.close()

    def test_reopening_keeps_the_graph_version_and_restores_the_schema_version(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "graph.sqlite")
        first = open_connection(path)
        first.execute("UPDATE cgr_meta SET value = '7' WHERE name = 'graph_version'")
        first.execute("UPDATE cgr_meta SET value = '1' WHERE name = 'schema_version'")
        first.close()

        again = open_connection(path)
        try:
            assert _meta(again) == {
                ecs.EG_META_SCHEMA_VERSION: ecs.EG_SCHEMA_VERSION,
                ecs.EG_META_GRAPH_VERSION: "7",
            }
        finally:
            again.close()

    def test_a_connection_has_the_engines_functions_and_busy_timeout(self) -> None:
        conn = open_connection(":memory:")
        try:
            assert conn.execute("SELECT eg_range(1, 3, 1)").fetchone() == ("[1,2,3]",)
            assert conn.execute("PRAGMA busy_timeout").fetchone() == (60_000,)
        finally:
            conn.close()

    def test_a_read_only_connection_reads_but_cannot_write(
        self, tmp_path: Path
    ) -> None:
        path = str(tmp_path / "graph.sqlite")
        writer = open_connection(path)
        writer.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'a')")
        writer.close()

        reader = open_connection(path, read_only=True)
        try:
            assert reader.execute("SELECT label, key FROM cgr_nodes").fetchall() == [
                ("T", "a")
            ]
            assert reader.execute("SELECT eg_range(1, 2, 1)").fetchone() == ("[1,2]",)
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'b')")
        finally:
            reader.close()

    def test_a_read_only_open_keeps_uri_characters_in_the_path_literal(
        self, tmp_path: Path
    ) -> None:
        # Read-only opens go through a `file:` URI, where `?`, `#` and `%`
        # would otherwise start a query, a fragment or an escape.
        path = str(tmp_path / "odd?dir#100%" / "graph.sqlite")
        writer = open_connection(path)
        writer.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'a')")
        writer.close()

        reader = open_connection(path, read_only=True)
        try:
            assert reader.execute("SELECT count(*) FROM cgr_nodes").fetchone() == (1,)
        finally:
            reader.close()

    def test_a_too_old_sqlite_is_refused_before_the_file_is_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 8, 0))
        target = tmp_path / "never" / "graph.sqlite"
        with pytest.raises(EmbeddedGraphUnsupported):
            open_connection(str(target))
        assert not target.parent.exists()

    def test_opening_a_schema_1_file_moves_it_to_autoincrement(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "old.sqlite"
        with sqlite3.connect(path) as old:
            old.executescript(_SCHEMA_1_NODES)
        old.close()

        conn = open_connection(str(path))
        try:
            assert ecs.EG_AUTOINCREMENT in _nodes_ddl(conn).upper()
            conn.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'new')")
            (new_id,) = conn.execute(
                "SELECT id FROM cgr_nodes WHERE key = 'new'"
            ).fetchone()
        finally:
            conn.close()
        # The sequence starts past the largest copied id, not past a gap.
        assert new_id == 10


class TestStopNodeIdReuse:
    def test_a_schema_1_table_keeps_its_ids_rows_and_indexes(
        self, raw: sqlite3.Connection
    ) -> None:
        raw.executescript(_SCHEMA_1_NODES)

        stop_node_id_reuse(raw)

        assert ecs.EG_AUTOINCREMENT in _nodes_ddl(raw).upper()
        assert raw.execute(
            "SELECT id, label, key, props FROM cgr_nodes ORDER BY id"
        ).fetchall() == [
            (4, "Function", "m.a", '{"name": "a"}'),
            (9, "Function", "m.b", '{"name": "b"}'),
        ]
        indexes = {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'cgr_nodes'"
            )
        }
        assert "cgr_nodes_label_key" in indexes
        assert not raw.in_transaction
        # The rebuilt table took the old name; no scratch table is left.
        assert raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = ?",
            (ecs.EG_NODES_REBUILD_TABLE,),
        ).fetchone() == (0,)

    def test_a_current_table_is_left_alone(self, raw: sqlite3.Connection) -> None:
        raw.executescript(ecs.EG_SCHEMA_SQL)
        raw.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'a')")
        before = _nodes_ddl(raw)

        stop_node_id_reuse(raw)

        assert _nodes_ddl(raw) == before
        assert raw.execute("SELECT key FROM cgr_nodes").fetchall() == [("a",)]

    def test_a_file_without_a_node_table_is_left_alone(
        self, raw: sqlite3.Connection
    ) -> None:
        stop_node_id_reuse(raw)

        assert raw.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)

    def test_a_failed_rebuild_rolls_back_to_the_old_table(
        self, raw: sqlite3.Connection
    ) -> None:
        raw.executescript(_SCHEMA_1_NODES)
        # A scratch table left with the wrong columns: CREATE IF NOT EXISTS
        # keeps it, so the copy into it fails halfway through the rebuild.
        raw.execute(f"CREATE TABLE {ecs.EG_NODES_REBUILD_TABLE}(id INTEGER)")
        before = _nodes_ddl(raw)

        with pytest.raises(sqlite3.OperationalError):
            stop_node_id_reuse(raw)

        assert not raw.in_transaction
        assert _nodes_ddl(raw) == before
        assert raw.execute("SELECT id FROM cgr_nodes ORDER BY id").fetchall() == [
            (4,),
            (9,),
        ]


class TestPinPlannerStatistics:
    def test_a_measured_analyze_is_replaced_by_the_reference_statistics(
        self, raw: sqlite3.Connection
    ) -> None:
        raw.executescript(ecs.EG_SCHEMA_SQL)
        raw.execute("INSERT INTO cgr_nodes(label, key) VALUES ('T', 'a'), ('T', 'b')")
        raw.execute("ANALYZE")
        assert _stats(raw) - set(ecs.EG_PLANNER_STATISTICS), (
            "fixture guard: the ANALYZE must have measured something else"
        )

        pin_planner_statistics(raw)

        graph_tables = {ecs.EG_NODES_TABLE, ecs.EG_EDGES_TABLE}
        pinned = {row for row in _stats(raw) if row[0] in graph_tables}
        assert pinned == set(ecs.EG_PLANNER_STATISTICS)

    def test_statistics_of_other_tables_survive(self, raw: sqlite3.Connection) -> None:
        raw.executescript(ecs.EG_SCHEMA_SQL)
        raw.execute("CREATE TABLE side(v INTEGER)")
        raw.execute("CREATE INDEX side_v ON side(v)")
        raw.executemany("INSERT INTO side(v) VALUES (?)", [(1,), (2,), (2,)])
        raw.execute("ANALYZE side")
        side = {row for row in _stats(raw) if row[0] == "side"}
        assert side, "fixture guard: ANALYZE must record the side table"

        pin_planner_statistics(raw)

        assert side <= _stats(raw)

    def test_pinning_works_before_anything_was_analyzed(
        self, raw: sqlite3.Connection
    ) -> None:
        raw.executescript(ecs.EG_SCHEMA_SQL)
        assert raw.execute(
            "SELECT count(*) FROM sqlite_master WHERE name = 'sqlite_stat1'"
        ).fetchone() == (0,)

        pin_planner_statistics(raw)

        assert _stats(raw) == set(ecs.EG_PLANNER_STATISTICS)
