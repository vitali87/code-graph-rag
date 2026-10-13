"""The optional Rust extension (`cgr_graph_rs`) and the engine's paths into it.

rust/cgr_graph_rs runs the whole-graph procedures (procedures.py) in Rust,
returning exactly what the Python implementations return. Those stay the
reference, and run whenever the extension is not installed or the graph has
no file another connection could open (`:memory:`).

The extension reads on its own connection, which sees committed data only
and need not share the calling statement's snapshot: another process may
commit in between. So it reads only inside statements that write nothing,
and only at the graph version the statement's own transaction reads (every
graph-changing transaction bumps `cgr_meta.graph_version`); on a newer graph
it declines and the engine answers from its own snapshot. It is handed the
statement's remaining time too, and stops where the Python code would check
its deadline (limits.py), raising what the engine raises.

Two SQLite libraries then share the process, Python's and the extension's
own. Each knows only the POSIX locks it took, and closing any descriptor of a
file drops every lock the process holds on it (sqlite.org/howtocorrupt.html,
"multiple copies of SQLite linked into the same application"). So the
extension's connections to a file are opened once, kept while any engine
connection to the file is open in this process and closed after the last; and
the engine's statements on one file, inside which the extension reads, take
turns under one lock, so neither library works inside a transaction of the
other's.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..constants.embedded_graph import (
    EG_BUSY_TIMEOUT_MS,
    EG_CACHE_SIZE_KIB,
    EG_ERR_TIMEOUT,
    EG_MEMORY_PATH,
    EG_RUST_READER,
)
from . import sql as sq
from .extension import rust_extension
from .limits import QueryTimeout, seconds_left
from .types import SqlValue

if TYPE_CHECKING:
    from .procedures import Record

# Which paths may use the extension; the parity tests and benchmarks flip
# them to compare it with the Python path (docs/architecture/embedded-graph.md
# has the numbers).
PROCEDURES: bool = True


class Reader(Protocol):
    def run_procedure(
        self,
        version: str | None,
        name: str,
        args: list[SqlValue],
        seconds_left: float | None,
    ) -> list[Record] | None: ...

    def close(self) -> None: ...


# ------------------------------------------------------------------- files


@dataclass
class _File:
    """The extension's handles on one file, shared by its engine connections."""

    path: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0
    reader: Reader | None = None


_FILES: dict[tuple[int, int], _File] = {}
# Each open engine connection's graph by id() of its sqlite3 connection, for
# the procedures, which are handed only that connection.
_GRAPHS: dict[int, NativeGraph] = {}
_FILES_GUARD = threading.Lock()


class NativeGraph:
    """The extension's view of the file behind one engine connection."""

    def __init__(
        self, conn: sqlite3.Connection, key: tuple[int, int], file: _File
    ) -> None:
        self._conn = conn
        self._key = key
        self._file = file

    @property
    def lock(self) -> threading.RLock:
        return self._file.lock

    def _reader(self) -> Reader | None:
        with _FILES_GUARD:
            if self._file.reader is None:
                self._file.reader = _new_reader(self._file.path)
            return self._file.reader

    def run_procedure(self, name: str, args: Sequence[SqlValue]) -> list[Record] | None:
        """A ported procedure's records at this connection's snapshot."""
        reader = self._reader() if PROCEDURES else None
        if reader is None:
            return None
        version = _snapshot_version(self._conn)
        try:
            return reader.run_procedure(version, name, list(args), seconds_left())
        except TimeoutError as error:
            raise QueryTimeout(EG_ERR_TIMEOUT) from error

    def release(self) -> None:
        """Forget this connection; after the file's last, close the handles."""
        with _FILES_GUARD:
            _GRAPHS.pop(id(self._conn), None)
            self._file.users -= 1
            if self._file.users:
                return
            _FILES.pop(self._key, None)
            reader, self._file.reader = self._file.reader, None
        if reader is not None:
            reader.close()


def _new_reader(path: str) -> Reader | None:
    make = getattr(rust_extension(), EG_RUST_READER, None)
    if make is None:
        return None
    return make(path, EG_BUSY_TIMEOUT_MS, EG_CACHE_SIZE_KIB, sq.GRAPH_VERSION)


def _snapshot_version(conn: sqlite3.Connection) -> str | None:
    """The graph version `conn`'s open transaction reads.

    Inside a statement's transaction its snapshot is already fixed by the
    reads before it; for a lone CALL this read fixes it.
    """
    row = conn.execute(sq.GRAPH_VERSION).fetchone()
    return row[0] if row else None


def attach(conn: sqlite3.Connection, path: str) -> NativeGraph | None:
    """The extension's handles for a new engine connection to `path`."""
    if rust_extension() is None or path == EG_MEMORY_PATH:
        return None
    target = Path(path).expanduser().resolve()
    try:
        stat = target.stat()
    except OSError:
        return None
    key = (stat.st_dev, stat.st_ino)
    with _FILES_GUARD:
        file = _FILES.setdefault(key, _File(str(target)))
        file.users += 1
        graph = _GRAPHS[id(conn)] = NativeGraph(conn, key, file)
    return graph


def release(graph: NativeGraph | None) -> None:
    """Call once the engine connection `graph` was attached to is closed."""
    if graph is not None:
        graph.release()


def turn(graph: NativeGraph | None) -> AbstractContextManager[object]:
    """Held around each engine statement on a file the extension also opens."""
    return graph.lock if graph is not None else nullcontext()


def run_procedure(
    conn: sqlite3.Connection, name: str, args: Sequence[SqlValue]
) -> list[Record] | None:
    """A procedure's records from the extension, or None to run it in Python.

    None for a procedure not ported, a graph with no file, or one another
    writer changed after the calling statement began reading it.
    """
    graph = _GRAPHS.get(id(conn))
    return graph.run_procedure(name, args) if graph is not None else None
