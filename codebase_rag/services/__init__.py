from typing import Protocol, runtime_checkable

from ..types_defs import PropertyDict, PropertyParams, PropertyValue, ResultRow


@runtime_checkable
class IngestorProtocol(Protocol):
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None: ...

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None: ...

    def flush_all(self) -> None: ...


@runtime_checkable
class QueryProtocol(Protocol):
    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]: ...

    def execute_write(
        self, query: str, params: PropertyParams | None = None
    ) -> None: ...


@runtime_checkable
class ReadOnlyQueryProtocol(QueryProtocol, Protocol):
    """A graph that can also run an untrusted query without letting it write."""

    def fetch_read_only(self, query: str) -> list[ResultRow]: ...


@runtime_checkable
class QueryingIngestorProtocol(IngestorProtocol, QueryProtocol, Protocol):
    """A sink that can also read back what it wrote."""


from .filtering import FilteringIngestor  # noqa: E402

__all__ = [
    "IngestorProtocol",
    "QueryingIngestorProtocol",
    "QueryProtocol",
    "ReadOnlyQueryProtocol",
    "FilteringIngestor",
]
