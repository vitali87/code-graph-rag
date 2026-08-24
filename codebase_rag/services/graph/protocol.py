from __future__ import annotations

import types
from typing import Protocol, runtime_checkable

from ...types_defs import GraphData
from .. import IngestorProtocol, QueryProtocol


@runtime_checkable
class GraphIngestor(IngestorProtocol, QueryProtocol, Protocol):
    """The full storage surface. Extends the node/relationship sink
    (IngestorProtocol) and the read/write query surface (QueryProtocol)
    that already existed, adding lifecycle and admin operations."""

    batch_size: int

    def __enter__(self) -> GraphIngestor: ...

    def __exit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None: ...

    async def __aenter__(self) -> GraphIngestor: ...

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: Exception | None,
        exc_tb: types.TracebackType | None,
    ) -> None: ...

    def ensure_constraints(self) -> None: ...

    def flush_nodes(self) -> None: ...

    def flush_relationships(self) -> None: ...

    def clean_database(self) -> None: ...

    def list_projects(self) -> list[str]: ...

    def list_project_roots(self) -> dict[str, str | None]: ...

    def delete_project(self, project_name: str) -> None: ...

    def export_graph_to_dict(self) -> GraphData: ...
