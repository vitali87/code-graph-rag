from __future__ import annotations

from typing import TYPE_CHECKING

from ... import exceptions as ex
from ...constants import (
    ARCADE_ALLOWED_PROCEDURE_PREFIXES,
    ARCADE_BENIGN_SUBSTRINGS,
    ARCADE_DDL_EDGE_TYPE,
    ARCADE_DDL_PROPERTY,
    ARCADE_DDL_UNIQUE_INDEX,
    ARCADE_DDL_VERTEX_TYPE,
    ARCADE_PROCEDURE_CATALOG,
    ARCADE_RETRYABLE_SUBSTRINGS,
    NODE_UNIQUE_CONSTRAINTS,
    GraphBackend,
    NodeLabel,
    RelationshipType,
)
from .arcade_http import ArcadeHttpClient

if TYPE_CHECKING:
    from .protocol import GraphIngestor


def build_arcade_schema_statements() -> list[str]:
    """The full DDL bootstrap, ordered so each statement's dependencies exist.

    Derived entirely from the existing constant tables: the guard in
    constants/graph.py that rejects any NodeLabel without a unique key keeps
    this in sync with the schema for free, so there is no second list to
    maintain.
    """
    statements: list[str] = []

    for label in NodeLabel:
        statements.append(ARCADE_DDL_VERTEX_TYPE.format(label=label.value))

    # ArcadeDB refuses CREATE INDEX on an undeclared property, so every
    # property declaration precedes every index.
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_PROPERTY.format(label=label, prop=prop))
    for label, prop in NODE_UNIQUE_CONSTRAINTS.items():
        statements.append(ARCADE_DDL_UNIQUE_INDEX.format(label=label, prop=prop))

    for rel_type in RelationshipType:
        statements.append(ARCADE_DDL_EDGE_TYPE.format(rel_type=rel_type.value))

    return statements


class ArcadeDBDialect:
    __slots__ = ("_http",)

    def __init__(self, http: ArcadeHttpClient | None = None) -> None:
        self._http = http

    @property
    def name(self) -> GraphBackend:
        return GraphBackend.ARCADEDB

    def ensure_schema(self, ingestor: GraphIngestor) -> None:
        if self._http is None:
            raise ex.ArcadeHttpError(ex.ARCADE_NO_HTTP_CLIENT)
        for statement in build_arcade_schema_statements():
            try:
                self._http.sql(statement)
            except Exception as exc:
                if not self.is_benign_error(exc):
                    raise

    def apply_query_limit(self, query: str, mb: int) -> str:
        # No per-query memory cap exists here. ArcadeDBIngestor bounds reads
        # with a transaction timeout instead; see settings.ARCADEDB_TX_TIMEOUT_S.
        return query

    @property
    def procedure_catalog(self) -> str:
        return ARCADE_PROCEDURE_CATALOG

    @property
    def allowed_proc_prefixes(self) -> frozenset[str]:
        return ARCADE_ALLOWED_PROCEDURE_PREFIXES

    def is_benign_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_BENIGN_SUBSTRINGS)

    def is_retryable(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return any(s in text for s in ARCADE_RETRYABLE_SUBSTRINGS)
