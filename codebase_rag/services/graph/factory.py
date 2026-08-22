from __future__ import annotations

from ... import exceptions as ex
from ...config import settings
from ...constants import GraphBackend
from ...utils.dependencies import has_neo4j_driver
from .dialect import GraphDialect
from .memgraph import MemgraphDialect, MemgraphIngestor
from .protocol import GraphIngestor

_ARCADEDB_EXTRA = "arcadedb"


def get_dialect(backend: GraphBackend | None = None) -> GraphDialect:
    resolved = backend or settings.GRAPH_BACKEND
    if resolved == GraphBackend.ARCADEDB:
        from .arcadedb import ArcadeDBDialect

        return ArcadeDBDialect()
    return MemgraphDialect()


def get_ingestor(
    backend: GraphBackend | None = None,
    batch_size: int | None = None,
) -> GraphIngestor:
    """Build the configured ingestor.

    Unlike `_get_vector_store()`, which returns None and warns when its
    dependency is missing, a missing graph backend is not degradable — it
    is the whole product — so this raises instead.
    """
    resolved = backend or settings.GRAPH_BACKEND
    size = settings.MEMGRAPH_BATCH_SIZE if batch_size is None else batch_size

    if resolved == GraphBackend.ARCADEDB:
        if not has_neo4j_driver():
            raise ex.GraphBackendUnavailableError(
                ex.GRAPH_BACKEND_UNAVAILABLE.format(
                    backend=resolved, extra=_ARCADEDB_EXTRA
                )
            )
        if not settings.ARCADEDB_USERNAME or not settings.ARCADEDB_PASSWORD:
            raise ValueError(ex.GRAPH_BACKEND_AUTH_REQUIRED.format(backend=resolved))
        from .arcadedb import ArcadeDBIngestor

        return ArcadeDBIngestor(
            host=settings.ARCADEDB_HOST,
            bolt_port=settings.ARCADEDB_BOLT_PORT,
            http_port=settings.ARCADEDB_HTTP_PORT,
            database=settings.ARCADEDB_DATABASE,
            username=settings.ARCADEDB_USERNAME,
            password=settings.ARCADEDB_PASSWORD,
            batch_size=size,
        )

    return MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=size,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
    )
