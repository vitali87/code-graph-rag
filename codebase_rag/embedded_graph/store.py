"""The SQLite file behind the embedded graph: schema, pragmas, connections."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..constants.embedded_graph import (
    EG_BUSY_TIMEOUT_MS,
    EG_BYTES_PER_MB,
    EG_CACHE_SIZE_KIB,
    EG_ERR_SQLITE_VERSION,
    EG_MEMORY_PATH,
    EG_META_SCHEMA_VERSION,
    EG_META_UPSERT,
    EG_PLANNER_STATISTICS,
    EG_PRAGMA_BUSY,
    EG_PRAGMA_CACHE,
    EG_PRAGMA_HEAP_LIMIT,
    EG_PRAGMAS,
    EG_SCHEMA_SQL,
    EG_SCHEMA_VERSION,
    EG_SQL_CLEAR_STATS,
    EG_SQL_CREATE_STATS,
    EG_SQL_INSERT_STAT,
    EG_SQL_JSON_PROBE,
    EG_SQL_RELOAD_STATS,
    EG_SQL_SEED_GRAPH_VERSION,
    EG_STATEMENT_CACHE_SIZE,
    EG_URI_READ_ONLY,
    EG_VERSION_SEPARATOR,
)
from . import functions

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
            EG_ERR_SQLITE_VERSION.format(
                need=EG_VERSION_SEPARATOR.join(map(str, MIN_SQLITE_VERSION)),
                have=sqlite3.sqlite_version,
            )
        )
    probe = sqlite3.connect(EG_MEMORY_PATH)
    try:
        probe.execute(EG_SQL_JSON_PROBE)
    except sqlite3.OperationalError as e:
        raise EmbeddedGraphUnsupported(
            EG_ERR_SQLITE_VERSION.format(
                need=EG_VERSION_SEPARATOR.join(map(str, MIN_SQLITE_VERSION)),
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
    conn.execute(EG_SQL_CREATE_STATS)
    conn.execute(EG_SQL_CLEAR_STATS)
    conn.executemany(EG_SQL_INSERT_STAT, EG_PLANNER_STATISTICS)
    conn.execute(EG_SQL_RELOAD_STATS)


def open_connection(
    path: str, read_only: bool = False, heap_limit_mb: int | None = None
) -> sqlite3.Connection:
    """One connection to the graph file, configured for the engine.

    Autocommit (`isolation_level=None`): the executor opens its own
    transactions, IMMEDIATE for anything that writes, so two processes that
    both write queue on the busy timeout instead of failing to upgrade a lock.
    """
    check_sqlite()
    if path != EG_MEMORY_PATH:
        Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        target = str(Path(path).expanduser())
    else:
        target = path
    if read_only and target != EG_MEMORY_PATH:
        # A URI built by pathlib, so `?`, `#` or `%` in a path stay literal.
        conn = sqlite3.connect(
            Path(target).resolve().as_uri() + EG_URI_READ_ONLY,
            uri=True,
            isolation_level=None,
            check_same_thread=False,
            cached_statements=EG_STATEMENT_CACHE_SIZE,
        )
    else:
        conn = sqlite3.connect(
            target,
            isolation_level=None,
            check_same_thread=False,
            cached_statements=EG_STATEMENT_CACHE_SIZE,
        )
    conn.execute(EG_PRAGMA_BUSY.format(ms=EG_BUSY_TIMEOUT_MS))
    conn.execute(EG_PRAGMA_CACHE.format(kib=EG_CACHE_SIZE_KIB))
    if heap_limit_mb is not None:
        conn.execute(EG_PRAGMA_HEAP_LIMIT.format(bytes=heap_limit_mb * EG_BYTES_PER_MB))
    if not read_only:
        for pragma in EG_PRAGMAS:
            conn.execute(pragma)
        conn.executescript(EG_SCHEMA_SQL)
        conn.execute(EG_META_UPSERT, (EG_META_SCHEMA_VERSION, EG_SCHEMA_VERSION))
        conn.execute(EG_SQL_SEED_GRAPH_VERSION)
        pin_planner_statistics(conn)
    functions.register(conn)
    return conn
