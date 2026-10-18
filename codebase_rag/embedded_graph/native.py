"""The optional Rust extension (`cgr_graph_rs`) and the engine's paths into it.

rust/cgr_graph_rs does in Rust what the engine otherwise does in Python and
SQL: the whole-graph procedures (procedures.py), variable-length walks, the
ingestor's batch writes and the decoding of result rows. Each Rust path
returns exactly what the Python path returns. The Python path stays the
reference, and runs whenever the extension is not installed, the graph has no
file another connection could open (`:memory:`), or the input holds a value
the Rust path cannot reproduce exactly: it declines rather than approximates.

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
the engine's statements and the extension's writes on one file take turns
under one lock, so neither library works inside a transaction of the other's.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, Protocol

from loguru import logger

from .. import logs as ls
from ..constants.embedded_graph import (
    EG_BUSY_TIMEOUT_MS,
    EG_CACHE_SIZE_KIB,
    EG_ERR_TIMEOUT,
    EG_MEMORY_PATH,
    EG_PRAGMAS,
    EG_RUST_DECODE_ROWS,
    EG_RUST_READER,
    EG_RUST_WRITER,
    EgDecode,
    EgSqlFn,
)
from ..constants.graph import (
    KEY_FROM_VAL,
    KEY_ID,
    KEY_PROPS,
    KEY_ROW_INDEX,
    KEY_TO_VAL,
    NODE_UNIQUE_CONSTRAINTS,
)
from ..types_defs import NodeBatchRow, RelBatchRow, ResultValue
from . import sql as sq
from .extension import rust_extension
from .limits import QueryTimeout, seconds_left
from .types import SqlValue

if TYPE_CHECKING:
    from .procedures import Record

# Which paths may use the extension; the parity tests and benchmarks flip
# them to compare it with the Python path (docs/architecture/embedded-graph.md
# has the numbers). Walks stay off: the extension walks row for row as the
# CTE does, but handing the rows back to SQLite (about 1.2 us each through
# json_each) costs what SQLite's own walk of cgr's tree-shaped containment
# costs, so cgr's shipped walks ran 0.8-1.3x as fast; and SQLite plans the
# joins around the two differently, so an unordered result can come back in
# another order.
PROCEDURES: bool = True
WALKS: bool = False
INGEST: bool = True
DECODE: bool = True

# The extension's decode kinds; any other kind passes the value through.
_DECODE_CODES = {EgDecode.BOOL: 1, EgDecode.JSON: 2, EgDecode.NULL: 3}

type Pattern = tuple[str, str, str, str, str]


class Reader(Protocol):
    def walk(
        self,
        seconds_left: float | None,
        seeds: str,
        version: str | None,
        direction: str,
        max_depth: int | None,
        *types: str,
    ) -> str | None: ...

    def run_procedure(
        self,
        version: str | None,
        name: str,
        args: list[SqlValue],
        seconds_left: float | None,
    ) -> list[Record] | None: ...

    def close(self) -> None: ...


class RelFields(NamedTuple):
    """The field names of the ingestor's relationship rows."""

    from_val: str
    to_val: str
    props: str
    row_index: str


class Writer(Protocol):
    def merge_node_rows(
        self,
        label: str,
        key_prop: str,
        key_path: str,
        id_field: str,
        props_field: str,
        rows: list[NodeBatchRow],
    ) -> bool: ...

    def merge_relationship_rows(
        self,
        from_label: str,
        to_label: str,
        rel_type: str,
        merge_keys: list[str],
        merge_paths: list[str],
        has_props: bool,
        fields: RelFields,
        rows: list[RelBatchRow],
    ) -> int | None: ...

    def close(self) -> None: ...


type RowDecoder = Callable[
    [list[tuple[SqlValue, ...]], list[int], Callable[[str], ResultValue]],
    list[tuple[ResultValue, ...]],
]


@cache
def _decoder() -> RowDecoder | None:
    return getattr(rust_extension(), EG_RUST_DECODE_ROWS, None)


# ------------------------------------------------------------------- files


@dataclass
class _File:
    """The extension's handles on one file, shared by its engine connections."""

    path: str
    lock: threading.RLock = field(default_factory=threading.RLock)
    users: int = 0
    reader: Reader | None = None
    writer: Writer | None = None


_REL_FIELDS = RelFields(KEY_FROM_VAL, KEY_TO_VAL, KEY_PROPS, KEY_ROW_INDEX)
_FILES: dict[tuple[int, int], _File] = {}
# Each open engine connection's graph by id() of its sqlite3 connection, for
# the procedures, which are handed only that connection.
_GRAPHS: dict[int, NativeGraph] = {}
_FILES_GUARD = threading.Lock()


class RelMerge(NamedTuple):
    """What a relationship batch's MERGE query was built from."""

    merge_keys: tuple[str, ...]
    has_props: bool


class NativeGraph:
    """The extension's view of the file behind one engine connection."""

    def __init__(
        self, conn: sqlite3.Connection, key: tuple[int, int], file: _File
    ) -> None:
        self._conn = conn
        self._key = key
        self._file = file
        self.walks = False
        # Why the extension's last walk failed its statement, if it did.
        self.walk_saw_newer_graph = False
        self.walk_timed_out = False

    @property
    def lock(self) -> threading.RLock:
        return self._file.lock

    def _reader(self) -> Reader | None:
        with _FILES_GUARD:
            if self._file.reader is None:
                self._file.reader = _new_reader(self._file.path)
            return self._file.reader

    def _writer(self) -> Writer | None:
        with _FILES_GUARD:
            if self._file.writer is None:
                self._file.writer = _new_writer(self._file.path)
            return self._file.writer

    def register_walks(self) -> None:
        """Let this connection's compiled walks call the extension."""
        reader = self._reader()
        if reader is None:
            return
        self._conn.create_function(
            EgSqlFn.WALK, -1, _walk_function(self, reader), deterministic=True
        )
        self.walks = True

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
            handles = (self._file.reader, self._file.writer)
            self._file.reader = self._file.writer = None
        for handle in handles:
            if handle is not None:
                handle.close()

    def _write[R](self, work: Callable[[Writer], R]) -> R | None:
        writer = self._writer()
        if writer is None:
            return None
        with self._file.lock:
            # A transaction of the engine's own would not see this write.
            if self._conn.in_transaction:
                return None
            try:
                return work(writer)
            except RuntimeError as error:
                logger.debug(ls.EG_NATIVE_WRITE_FAILED.format(error=error))
                return None

    def merge_nodes(
        self, label: str, id_key: str, rows: Sequence[NodeBatchRow]
    ) -> bool:
        """`MERGE (n:label {id_key: row.id}) SET n += row.props` for each row.

        False leaves the batch to the engine, which then writes it (or fails
        on it) as it always has: the extension declines a row holding a value
        the engine would store differently from plain `json.dumps` (see
        rust/cgr_graph_rs/src/ingest.rs).
        """
        if not INGEST or NODE_UNIQUE_CONSTRAINTS.get(label) != id_key:
            return False
        path = sq.json_path(id_key)
        done = self._write(
            lambda w: w.merge_node_rows(
                label, id_key, path, KEY_ID, KEY_PROPS, list(rows)
            )
        )
        return bool(done)

    def merge_relationships(
        self, pattern: Pattern, spec: RelMerge, rows: Sequence[RelBatchRow]
    ) -> int | None:
        """The batch of `build_merge_relationship_query` for `pattern`.

        Returns what its RETURN counts, or None to leave it to the engine.
        """
        from_label, from_key, rel_type, to_label, to_key = pattern
        if (
            not INGEST
            or NODE_UNIQUE_CONSTRAINTS.get(from_label) != from_key
            or NODE_UNIQUE_CONSTRAINTS.get(to_label) != to_key
        ):
            return None
        paths = [sq.json_path(k) for k in spec.merge_keys]
        return self._write(
            lambda w: w.merge_relationship_rows(
                from_label,
                to_label,
                rel_type,
                list(spec.merge_keys),
                paths,
                spec.has_props,
                _REL_FIELDS,
                list(rows),
            )
        )


def _new_reader(path: str) -> Reader | None:
    make = getattr(rust_extension(), EG_RUST_READER, None)
    if make is None:
        return None
    return make(path, EG_BUSY_TIMEOUT_MS, EG_CACHE_SIZE_KIB, sq.GRAPH_VERSION)


def _new_writer(path: str) -> Writer | None:
    make = getattr(rust_extension(), EG_RUST_WRITER, None)
    if make is None:
        return None
    return make(
        path,
        EG_BUSY_TIMEOUT_MS,
        EG_CACHE_SIZE_KIB,
        list(EG_PRAGMAS),
        sq.BUMP_GRAPH_VERSION,
    )


class _NewerGraph(Exception):
    """Fails a statement whose walk found a newer graph than its own."""


def _walk_function(graph: NativeGraph, reader: Reader) -> Callable[..., str]:
    """What the walk SQL function calls: the reader's walk, handed the
    statement's remaining time (a seam the snapshot tests wrap).

    SQLite reports any exception a function raises as one generic error,
    so the reason a walk failed its statement is kept on `graph`.
    """

    def walk(
        seeds: str,
        version: str | None,
        direction: str,
        max_depth: int | None,
        *types: str,
    ) -> str:
        left = seconds_left()
        try:
            rows = reader.walk(left, seeds, version, direction, max_depth, *types)
        except TimeoutError:
            graph.walk_timed_out = True
            raise
        if rows is None:
            graph.walk_saw_newer_graph = True
            raise _NewerGraph
        return rows

    return walk


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
    if WALKS:
        graph.register_walks()
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


# ------------------------------------------------------------------- walks


def with_walks[R](graph: NativeGraph | None, run: Callable[[bool], R]) -> R:
    """`run(True)` lets a statement walk in the extension, `run(False)` in SQL.

    A walk that found a newer graph than its statement's fails the
    statement; nothing was written (walks run only in statements that write
    nothing), so it runs again, in the same transaction, with SQL's walks.
    One that ran out of time is the statement's timeout.
    """
    if graph is None or not graph.walks:
        return run(False)
    graph.walk_saw_newer_graph = graph.walk_timed_out = False
    try:
        return run(True)
    except sqlite3.OperationalError as error:
        if graph.walk_timed_out:
            raise QueryTimeout(EG_ERR_TIMEOUT) from error
        if not graph.walk_saw_newer_graph:
            raise
    return run(False)


# ---------------------------------------------------------------- decoding


def decode_rows(
    kinds: Sequence[EgDecode],
    rows: list[tuple[SqlValue, ...]],
    decode: Callable[[EgDecode, SqlValue], ResultValue],
) -> list[tuple[ResultValue, ...]]:
    """Each row's values decoded by their column's kind, as `decode` does."""
    accelerated = _decoder() if DECODE else None
    if accelerated is None:
        return [tuple(decode(k, v) for k, v in zip(kinds, row)) for row in rows]
    return accelerated(rows, [_DECODE_CODES.get(k, 0) for k in kinds], json.loads)
