from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ...constants import GraphBackend

if TYPE_CHECKING:
    from .protocol import GraphIngestor


@runtime_checkable
class GraphDialect(Protocol):
    """Everything that genuinely differs between graph engines.

    Deliberately small: the shared Cypher in cypher_queries.py runs on both
    backends unchanged, so only these six behaviours plus the name vary.
    """

    @property
    def name(self) -> GraphBackend: ...

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        """Create whatever the engine needs before MERGE is efficient:
        unique constraints and indexes on Memgraph; vertex/edge types,
        property declarations and unique indexes on ArcadeDB."""
        ...

    def apply_query_limit(self, query: str, mb: int) -> str:
        """Bound a read query's resource use, or return it unchanged when
        the engine has no equivalent."""
        ...

    @property
    def procedure_catalog(self) -> str:
        """The prompt fragment listing callable graph-algorithm procedures."""
        ...

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        """Procedure namespaces the read-only guard permits."""
        ...

    def is_benign_error(self, exc: Exception) -> bool:
        """True when the error means 'already done' and should not be logged."""
        ...

    def is_retryable(self, exc: Exception) -> bool:
        """True for transient write conflicts worth retrying."""
        ...
