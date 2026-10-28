from __future__ import annotations

from ..capture import CaptureSelection
from ..constants import NodeLabel, RelationshipType
from ..types_defs import PropertyDict, PropertyParams, PropertyValue, ResultRow
from . import IngestorProtocol, QueryingIngestorProtocol


class FilteringIngestor:
    """Wraps an ingestor and drops nodes/relationships that the capture
    selection excludes, so the ~20 parser emission sites stay untouched."""

    def __init__(self, inner: IngestorProtocol, selection: CaptureSelection) -> None:
        self._inner = inner
        self._selection = selection

    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        if self._selection.node_enabled(NodeLabel(label)):
            self._inner.ensure_node_batch(label, properties)

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        *args: PropertyDict | None,
        **kwargs: PropertyDict | None,
    ) -> None:
        # Transparent passthrough of the optional `properties` arg: emission
        # sites pass it positionally (INHERITS) or by keyword
        # (DEPENDS_ON_EXTERNAL), and downstream tests assert the exact call
        # shape, so the wrapper must not normalise one form into the other.
        if self._selection.rel_enabled(RelationshipType(rel_type)):
            self._inner.ensure_relationship_batch(
                from_spec, rel_type, to_spec, *args, **kwargs
            )

    def flush_all(self) -> None:
        self._inner.flush_all()

    def rel_enabled(self, rel_type: RelationshipType) -> bool:
        return self._selection.rel_enabled(rel_type)


class QueryingFilteringIngestor(FilteringIngestor):
    """The filter over a sink that can also be read.

    Separate from FilteringIngestor so `isinstance(sink, QueryProtocol)` tells
    the truth: the plain wrapper answered every read with [] and dropped every
    write when its sink was a write-only export, while still claiming to be
    queryable, so a caller's capability check could not tell the two apart.
    """

    def __init__(
        self, inner: QueryingIngestorProtocol, selection: CaptureSelection
    ) -> None:
        super().__init__(inner, selection)
        self._reader = inner

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._reader.fetch_all(query, params)

    def execute_write(self, query: str, params: PropertyParams | None = None) -> None:
        self._reader.execute_write(query, params)


def filtering_ingestor(
    inner: IngestorProtocol, selection: CaptureSelection
) -> FilteringIngestor:
    """The capture filter over `inner`, queryable exactly when `inner` is."""
    if isinstance(inner, QueryingIngestorProtocol):
        return QueryingFilteringIngestor(inner, selection)
    return FilteringIngestor(inner, selection)
