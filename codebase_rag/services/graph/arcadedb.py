from __future__ import annotations

from ...constants import (
    ARCADE_DDL_EDGE_TYPE,
    ARCADE_DDL_PROPERTY,
    ARCADE_DDL_UNIQUE_INDEX,
    ARCADE_DDL_VERTEX_TYPE,
    NODE_UNIQUE_CONSTRAINTS,
    NodeLabel,
    RelationshipType,
)


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
