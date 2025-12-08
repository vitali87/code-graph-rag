from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class IngestorProtocol(Protocol):
    """Protocol defining the interface for graph data ingestors."""

    def ensure_node_batch(self, label: str, properties: dict[str, Any]) -> None:
        """Adds a node to the buffer or processes it immediately."""
        ...

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, Any],
        rel_type: str,
        to_spec: tuple[str, str, Any],
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Adds a relationship to the buffer or processes it immediately."""
        ...

    def flush_all(self) -> None:
        """Flushes all buffered data."""
        ...


@runtime_checkable
class QueryProtocol(Protocol):
    """Protocol defining the interface for graph data querying."""

    def fetch_all(self, query: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Executes a query and fetches all results."""
        ...

    def execute_write(self, query: str, params: dict[str, Any] | None = None) -> None:
        """Executes a write query without returning results."""
        ...
