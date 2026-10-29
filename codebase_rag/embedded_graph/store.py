"""The SQLite file behind the embedded graph: schema, pragmas, connections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .. import constants as cs
from . import functions
from .sql import BEGIN_WRITE, COMMIT, ROLLBACK

# FILTER on aggregates and NULLS FIRST/LAST arrived in 3.30; json_each and
# friends must be compiled in (they are by default since 3.38, and in every
# mainstream build before that).
MIN_SQLITE_VERSION = (3, 30, 0)
ARROW_SQLITE_VERSION = (3, 38, 0)


class EmbeddedGraphUnsupported(RuntimeError):
    pass


def check_sqlite() -> None:
    have = sqlite3.sqlite_version_info
    if have < MIN_SQLITE_VERSION:
        raise EmbeddedGraphUnsupported(
            cs.EG_ERR_SQLITE_VERSION.format(
                need=cs.EG_VERSION_SEPARATOR.join(map(str, MIN_SQLITE_VERSION)),
                have=sqlite3.sqlite_version,
            )
        )
    probe = sqlite3.connect(cs.EG_MEMORY_PATH)
    try:
        probe.execute(cs.EG_SQL_JSON_PROBE)
    except sqlite3.OperationalError as e:
        raise EmbeddedGraphUnsupported(
            cs.EG_ERR_SQLITE_VERSION.format(
                need=cs.EG_VERSION_SEPARATOR.join(map(str, MIN_SQLITE_VERSION)),
                have=sqlite3.sqlite_version,
            )
        ) from e
    finally:
        probe.close()


def supports_json_arrow() -> bool:
    return sqlite3.sqlite_version_info >= ARROW_SQLITE_VERSION


def pin_planner_statistics(conn: sqlite3.Connection) -> None:
    """Give the planner the reference statistics (see EG_PLANNER_STATISTICS).

    Rewritten on every writable open, so an ANALYZE run by hand, or by an
    older cgr, cannot leave the planner judging the graph by a moment when
    it was nearly empty.
    """
    conn.execute(cs.EG_SQL_CREATE_STATS)
    conn.execute(cs.EG_SQL_CLEAR_STATS)
    conn.executemany(cs.EG_SQL_INSERT_STAT, cs.EG_PLANNER_STATISTICS)
    conn.execute(cs.EG_SQL_RELOAD_STATS)


def _reuses_node_ids(conn: sqlite3.Connection) -> bool:
    row = conn.execute(cs.EG_SQL_TABLE_DDL, (cs.EG_NODES_TABLE,)).fetchone()
    return row is not None and cs.EG_AUTOINCREMENT not in str(row[0]).upper()


def stop_node_id_reuse(conn: sqlite3.Connection) -> None:
    """Rebuild a schema-1 node table with AUTOINCREMENT (see EG_NODES_TABLE_SQL).

    SQLite cannot add AUTOINCREMENT to an existing table, so the rows move to
    a new one. The check repeats inside the write lock, so two processes
    opening the same old file rebuild it once.
    """
    if not _reuses_node_ids(conn):
        return
    conn.execute(BEGIN_WRITE)
    try:
        if _reuses_node_ids(conn):
            indexes = [
                row[0]
                for row in conn.execute(cs.EG_SQL_TABLE_INDEXES, (cs.EG_NODES_TABLE,))
            ]
            conn.execute(cs.EG_NODES_TABLE_SQL.format(table=cs.EG_NODES_REBUILD_TABLE))
            conn.execute(
                cs.EG_SQL_COPY_NODES.format(
                    dst=cs.EG_NODES_REBUILD_TABLE, src=cs.EG_NODES_TABLE
                )
            )
            conn.execute(cs.EG_SQL_DROP_TABLE.format(table=cs.EG_NODES_TABLE))
            conn.execute(
                cs.EG_SQL_RENAME_TABLE.format(
                    src=cs.EG_NODES_REBUILD_TABLE, dst=cs.EG_NODES_TABLE
                )
            )
            for index in indexes:
                conn.execute(index)
        conn.execute(COMMIT)
    except BaseException:
        conn.execute(ROLLBACK)
        raise


def open_connection(
    path: str, read_only: bool = False, heap_limit_mb: int | None = None
) -> sqlite3.Connection:
    """One connection to the graph file, configured for the engine.

    Autocommit (`isolation_level=None`): the executor opens its own
    transactions, IMMEDIATE for anything that writes, so two processes that
    both write queue on the busy timeout instead of failing to upgrade a lock.
    """
    check_sqlite()
    if path != cs.EG_MEMORY_PATH:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        target = str(Path(path).expanduser())
    else:
        target = path
    if read_only and target != cs.EG_MEMORY_PATH:
        # A URI built by pathlib, so `?`, `#` or `%` in a path stay literal.
        conn = sqlite3.connect(
            Path(target).resolve().as_uri() + cs.EG_URI_READ_ONLY,
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            cached_statements=cs.EG_STATEMENT_CACHE_SIZE,
        )
    else:
        conn = sqlite3.connect(
            target,
            isolation_level=None,
            check_same_thread=False,
            cached_statements=cs.EG_STATEMENT_CACHE_SIZE,
        )
    conn.execute(cs.EG_PRAGMA_BUSY.format(ms=cs.EG_BUSY_TIMEOUT_MS))
    conn.execute(cs.EG_PRAGMA_CACHE.format(kib=cs.EG_CACHE_SIZE_KIB))
    if heap_limit_mb is not None:
        conn.execute(
            cs.EG_PRAGMA_HEAP_LIMIT.format(bytes=heap_limit_mb * cs.EG_BYTES_PER_MB)
        )
    if not read_only:
        for pragma in cs.EG_PRAGMAS:
            conn.execute(pragma)
        stop_node_id_reuse(conn)
        conn.executescript(cs.EG_SCHEMA_SQL)
        conn.execute(
            cs.EG_META_UPSERT, (cs.EG_META_SCHEMA_VERSION, cs.EG_SCHEMA_VERSION)
        )
        conn.execute(cs.EG_SQL_SEED_GRAPH_VERSION)
        pin_planner_statistics(conn)
    functions.register(conn, heap_limit_mb)
    return conn
