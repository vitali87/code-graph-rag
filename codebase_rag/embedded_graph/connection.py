"""DB-API-shaped connection to the embedded graph.

`MemgraphIngestor` drives every engine through `ConnectionProtocol` /
`CursorProtocol` (types_defs); this is the embedded engine's implementation,
so the ingestor, its batching and every query constant work unchanged.
"""

from __future__ import annotations

import threading
import types
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..constants.embedded_graph import (
    EG_BATCH_PARAM,
    EG_ERR_CLOSED,
    EG_ERR_PARAMS_MAPPING,
)
from ..types_defs import (
    BatchParams,
    BatchWrapper,
    ColumnDescriptor,
    CypherParams,
    PropertyValue,
    ResultValue,
)
from . import native
from .executor import Executor, Result
from .store import open_connection, supports_json_arrow

if TYPE_CHECKING:
    # Annotation-only: TypeIs reaches `typing` in 3.13.
    from typing_extensions import TypeIs


def _is_batch(
    params: dict[str, PropertyValue] | BatchWrapper,
) -> TypeIs[BatchWrapper]:
    return len(params) == 1 and EG_BATCH_PARAM in params


def _as_params(params: dict[str, PropertyValue] | BatchWrapper) -> CypherParams:
    """The cursor's parameters as the plain mapping the engine binds from.

    A TypedDict is no `Mapping[str, ...]` to the type checker, though it
    is a dict at runtime, so the batch is re-wrapped under its own key.
    """
    if _is_batch(params):
        return {EG_BATCH_PARAM: params[EG_BATCH_PARAM]}
    return params


@dataclass(frozen=True, slots=True)
class _Column:
    name: str


class EmbeddedCursor:
    def __init__(self, conn: EmbeddedConnection) -> None:
        self._conn = conn
        self._rows: list[tuple[ResultValue, ...]] = []
        self._description: list[_Column] | None = None

    @property
    def description(self) -> Sequence[ColumnDescriptor] | None:
        return self._description

    def execute(
        self,
        query: str,
        params: dict[str, PropertyValue]
        | Sequence[BatchParams]
        | BatchWrapper
        | None = None,
    ) -> None:
        # The ingestor binds a mapping (`BatchWrapper` is one at runtime); a
        # bare sequence has no names to bind, as with the other engines.
        if params is None:
            plain = None
        elif isinstance(params, dict):
            plain = _as_params(params)
        else:
            raise TypeError(EG_ERR_PARAMS_MAPPING.format(kind=type(params).__name__))
        result = self._conn.run(query, plain)
        self._description = [_Column(c) for c in result.columns] or None
        self._rows = result.rows

    def fetchall(self) -> list[tuple[ResultValue, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self._rows = []


class EmbeddedConnection:
    def __init__(
        self, path: str, read_only: bool = False, heap_limit_mb: int | None = None
    ) -> None:
        self._sqlite = open_connection(path, read_only, heap_limit_mb)
        # The Rust extension's handles on this file, if it is installed.
        self.native = native.attach(self._sqlite, path)
        self._executor = Executor(
            self._sqlite, supports_json_arrow(), read_only, self.native
        )
        self._lock = threading.Lock()
        self._closed = False
        self.autocommit = True
        self.path = path

    def run(
        self,
        query: str,
        params: CypherParams | None = None,
        timeout_s: float | None = None,
    ) -> Result:
        """Run one statement; past `timeout_s` it stops with QueryTimeout."""
        if self._closed:
            raise ConnectionError(EG_ERR_CLOSED)
        with self._lock, native.turn(self.native):
            return self._executor.execute(query, params, timeout_s)

    def cursor(self) -> EmbeddedCursor:
        return EmbeddedCursor(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # The extension's handles go first: closing the file's last engine
        # connection checkpoints and deletes the WAL, which must not happen
        # while the extension's own SQLite still has the file open (native.py).
        with self._lock, native.turn(self.native):
            native.release(self.native)
            self._sqlite.close()

    def __enter__(self) -> EmbeddedConnection:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self.close()
