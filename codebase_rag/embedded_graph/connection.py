"""DB-API-shaped connection to the embedded graph.

`MemgraphIngestor` drives every engine through `ConnectionProtocol` /
`CursorProtocol` (types_defs); this is the embedded engine's implementation,
so the ingestor, its batching and every query constant work unchanged.
"""

from __future__ import annotations

import threading
import types
from collections.abc import Mapping, Sequence

from ..constants.embedded_graph import (
    EG_ERR_CLOSED,
    EG_ERR_PARAMS_MAPPING,
)
from ..types_defs import (
    BatchParams,
    BatchWrapper,
    ColumnDescriptor,
    PropertyValue,
    ResultValue,
)
from .executor import Executor, Result
from .store import open_connection, supports_json_arrow


class _Column:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


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
        if params is not None and not isinstance(params, Mapping):
            raise TypeError(EG_ERR_PARAMS_MAPPING.format(kind=type(params).__name__))
        result = self._conn.run(query, params)
        self._description = [_Column(c) for c in result.columns] or None
        self._rows = result.rows

    def fetchall(self) -> list[tuple[ResultValue, ...]]:
        return list(self._rows)

    def close(self) -> None:
        self._rows = []


class EmbeddedConnection:
    def __init__(self, path: str, read_only: bool = False) -> None:
        self._sqlite = open_connection(path, read_only=read_only)
        self._executor = Executor(self._sqlite, supports_json_arrow(), read_only)
        self._lock = threading.Lock()
        self._closed = False
        self.autocommit = True
        self.path = path

    def run(self, query: str, params: Mapping[str, object] | None = None) -> Result:
        # A mapping of anything JSON can hold; `bind_value` converts it.
        if self._closed:
            raise ConnectionError(EG_ERR_CLOSED)
        with self._lock:
            return self._executor.execute(query, params)

    def cursor(self) -> EmbeddedCursor:
        return EmbeddedCursor(self)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
