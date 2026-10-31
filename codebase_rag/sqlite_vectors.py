"""The SQLite file behind the `sqlite` vector store, through sqlite-vec.

The other backends talk to a client library; this module is that client for
a vec0 table, so `vector_store` can drive all three the same way. Its layout
is in `constants/sqlite_vectors.py`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from . import constants as cs
from . import exceptions as ex


class SqliteVectorRow(NamedTuple):
    """One point to write: its key, its payload and its vector."""

    point_id: str
    node_id: int
    qualified_name: str
    vector: list[float]


class SqliteVectorPoint(NamedTuple):
    point_id: str
    node_id: int


class SqliteVectorHit(NamedTuple):
    node_id: int
    distance: float
    qualified_name: str


def extensions_loadable() -> bool:
    # sqlite3 has no enable_load_extension unless CPython was configured with
    # --enable-loadable-sqlite-extensions, which some builds leave out.
    return hasattr(sqlite3.Connection, cs.SQLITE_LOAD_EXTENSION_METHOD)


def _float32(vector: Sequence[float]) -> bytes:
    from sqlite_vec import serialize_float32

    return serialize_float32(list(vector))


def open_sqlite_vector_client(path: Path) -> SqliteVectorClient:
    import sqlite_vec

    if not extensions_loadable():
        raise RuntimeError(ex.SQLITE_VEC_EXTENSIONS_UNSUPPORTED)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Autocommit: every write opens its own IMMEDIATE transaction (_write).
    # The MCP server searches from worker threads, hence check_same_thread.
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            # Left on, any SQL run later could load a library of its own.
            conn.enable_load_extension(False)
        conn.execute(cs.SQLITE_VEC_PRAGMA_BUSY.format(ms=cs.SQLITE_VEC_BUSY_TIMEOUT_MS))
        conn.execute(cs.SQLITE_VEC_PRAGMA_WAL)
        conn.execute(cs.SQLITE_VEC_CREATE_META)
    except Exception:
        conn.close()
        raise
    return SqliteVectorClient(conn)


def _create_collection(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(cs.SQLITE_VEC_CREATE_POINTS)
    conn.execute(cs.SQLITE_VEC_CREATE_POINTS_PROJECT_INDEX)
    conn.execute(cs.SQLITE_VEC_CREATE_VECTORS.format(dim=dim))
    conn.execute(cs.SQLITE_VEC_UPSERT_META, (cs.SQLITE_VEC_META_DIMENSION, str(dim)))


def _drop_collection(conn: sqlite3.Connection) -> None:
    conn.execute(cs.SQLITE_VEC_DROP_VECTORS)
    conn.execute(cs.SQLITE_VEC_DROP_POINTS)
    conn.execute(cs.SQLITE_VEC_DELETE_META, (cs.SQLITE_VEC_META_DIMENSION,))


class SqliteVectorClient:
    """One connection to the vectors file.

    A transaction belongs to the connection, not to the thread that opened
    it, so every call holds the lock: two searches from the MCP server's
    worker threads, or a search during a write, would otherwise interleave.
    """

    __slots__ = ("_conn", "_lock")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.Lock()

    @contextmanager
    def _write(self) -> Generator[sqlite3.Connection]:
        # The connection's own context manager commits, or rolls back when
        # the block raises, so a failed batch leaves no half-written point.
        with self._lock, self._conn:
            self._conn.execute(cs.SQLITE_VEC_BEGIN_WRITE)
            yield self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def dimension(self) -> int | None:
        """The vectors' size, or None when the collection was never made."""
        with self._lock:
            row = self._conn.execute(
                cs.SQLITE_VEC_SELECT_META, (cs.SQLITE_VEC_META_DIMENSION,)
            ).fetchone()
        return None if row is None else int(row[0])

    def create_collection(self, dim: int) -> None:
        with self._write() as conn:
            _create_collection(conn, dim)

    def recreate_collection(self, dim: int) -> None:
        # One transaction: a rebuild that fails keeps the old collection
        # rather than leaving none.
        with self._write() as conn:
            _drop_collection(conn)
            _create_collection(conn, dim)

    def upsert(self, project: str, rows: Sequence[SqliteVectorRow]) -> None:
        with self._write() as conn:
            for row in rows:
                conn.execute(
                    cs.SQLITE_VEC_UPSERT_POINT,
                    (row.point_id, row.node_id, row.qualified_name, project),
                )
                (rowid,) = conn.execute(
                    cs.SQLITE_VEC_POINT_ROWID, (row.point_id,)
                ).fetchone()
                conn.execute(cs.SQLITE_VEC_DELETE_VECTOR, (rowid,))
                # A zero vector has no direction, so its cosine distance is
                # NULL, which vec0 ranks ahead of every real neighbour; kept
                # out of the KNN table, it is stored but matches no query.
                if any(row.vector):
                    conn.execute(
                        cs.SQLITE_VEC_INSERT_VECTOR, (rowid, _float32(row.vector))
                    )

    def search(self, query: Sequence[float], limit: int) -> list[SqliteVectorHit]:
        """The `limit` nearest points, nearest first, by cosine distance."""
        k = min(limit, cs.SQLITE_VEC_MAX_K)
        with self._lock:
            rows = self._conn.execute(cs.SQLITE_VEC_SEARCH, (_float32(query), k))
            # A zero query has no direction either: every distance is NULL.
            return [
                SqliteVectorHit(node_id, distance, qualified_name)
                for node_id, distance, qualified_name in rows
                if distance is not None
            ]

    def project_points(self, project: str) -> list[SqliteVectorPoint]:
        with self._lock:
            rows = self._conn.execute(cs.SQLITE_VEC_PROJECT_POINTS, (project,))
            return [SqliteVectorPoint(*row) for row in rows]

    def points(self, point_ids: Sequence[str]) -> list[SqliteVectorPoint]:
        with self._lock:
            rows = self._conn.execute(
                cs.SQLITE_VEC_POINTS_BY_ID, (json.dumps(list(point_ids)),)
            )
            return [SqliteVectorPoint(*row) for row in rows]

    def delete(self, point_ids: Sequence[str]) -> None:
        if not point_ids:
            return
        ids = json.dumps(list(point_ids))
        with self._write() as conn:
            conn.execute(cs.SQLITE_VEC_DELETE_VECTORS_BY_POINT, (ids,))
            conn.execute(cs.SQLITE_VEC_DELETE_POINTS, (ids,))
