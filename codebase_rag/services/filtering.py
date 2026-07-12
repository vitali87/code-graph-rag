from __future__ import annotations

from ..capture import CaptureSelection
from ..constants import NodeLabel, RelationshipType
from ..types_defs import PropertyDict, PropertyValue, ResultRow
from . import IngestorProtocol, QueryProtocol


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
        # (H) Transparent passthrough of the optional `properties` arg: emission
        # (H) sites pass it positionally (INHERITS) or by keyword
        # (H) (DEPENDS_ON_EXTERNAL), and downstream tests assert the exact call
        # (H) shape, so the wrapper must not normalise one form into the other.
        if self._selection.rel_enabled(RelationshipType(rel_type)):
            self._inner.ensure_relationship_batch(
                from_spec, rel_type, to_spec, *args, **kwargs
            )

    def flush_all(self) -> None:
        self._inner.flush_all()

    def fetch_all(
        self, query: str, params: PropertyDict | None = None
    ) -> list[ResultRow]:
        if isinstance(self._inner, QueryProtocol):
            return self._inner.fetch_all(query, params)
        return []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        if isinstance(self._inner, QueryProtocol):
            self._inner.execute_write(query, params)
