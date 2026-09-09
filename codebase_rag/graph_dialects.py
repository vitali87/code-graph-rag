"""Per-engine Cypher differences, isolated behind one protocol.

The query corpus is overwhelmingly portable: `MERGE`, `UNWIND $batch`,
`SET n += row.props`, label and relationship alternation, `type()` and
property-form `IS NULL` mean the same thing on every engine we target.
Only a handful of constructs actually diverge, and this module is the one
place that knows about them:

* index and constraint DDL, which shares no syntax between engines;
* listing constraints, where both the statement AND the result columns
  differ (Memgraph returns `label`, Neo4j returns `labelsOrTypes`);
* Memgraph's `QUERY MEMORY LIMIT` suffix, which has no Neo4j equivalent
  and is a syntax error there.

Adding an engine means adding a `GraphDialect` subclass and registering
it below -- no edits to the ingestor, the query builders, or their call
sites. That is the point of the indirection: issue #1590 asks for Neo4j,
but the same seam has to take the engine after it without reopening any
of this.

A dialect is pure and stateless: it maps a request for a statement onto
the text this engine accepts. It never executes anything, which keeps it
trivially testable without a server.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .constants import (
    CYPHER_MEMORY_LIMIT_SUFFIX,
    CYPHER_MEMORY_LIMIT_TOKEN,
    CYPHER_SEMICOLON,
)
from .types_defs import ResultRow

# Engine identifiers, used by settings and by the registry below.
DIALECT_MEMGRAPH = "memgraph"
DIALECT_NEO4J = "neo4j"


def _constraint_name(label: str, prop: str) -> str:
    """A deterministic name for a constraint or index.

    Neo4j identifies both by name rather than by pattern -- `DROP
    CONSTRAINT` takes a name and nothing else -- so the name has to be
    derivable from the same (label, prop) pair every time, in any process,
    without reading the database first.
    """
    return f"cgr_{label.lower()}_{prop.lower()}"


@runtime_checkable
class GraphDialect(Protocol):
    """The engine-specific half of the Cypher this project emits."""

    name: str

    def create_constraint(self, label: str, prop: str) -> str:
        """Statement making `prop` unique across `label`."""
        ...

    def drop_constraint(
        self, label: str, prop: str, discovered_name: str | None = None
    ) -> str:
        """Statement removing the uniqueness of `prop` on `label`.

        `discovered_name` is the name the server reported for this
        constraint, where the engine addresses constraints by name. It
        matters because the constraint being dropped predates this code
        and may carry any name: dropping a name we derive ourselves would
        silently leave someone else's legacy constraint enforcing an
        obsolete key.
        """
        ...

    def create_index(self, label: str, prop: str) -> str:
        """Statement indexing `label` on `prop`."""
        ...

    def show_constraints(self) -> str:
        """Statement listing the constraints that currently exist."""
        ...

    def constraint_row_matches(self, row: ResultRow, label: str, prop: str) -> bool:
        """Whether a `show_constraints` row describes (label, prop).

        The row shape is part of the dialect: the two engines disagree on
        both the column name and whether the label arrives as a scalar or
        inside a list, so the comparison cannot live at the call site.
        """
        ...

    def constraint_row_name(self, row: ResultRow) -> str | None:
        """The server's own name for a `show_constraints` row, if any."""
        ...

    def apply_memory_limit(self, query: str, mb: int) -> str:
        """Bound a read's memory, where the engine supports it."""
        ...


class MemgraphDialect:
    """Memgraph 2.x/3.x, the engine this project shipped on first."""

    name = DIALECT_MEMGRAPH

    def create_constraint(self, label: str, prop: str) -> str:
        return f"CREATE CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"

    def drop_constraint(
        self, label: str, prop: str, discovered_name: str | None = None
    ) -> str:
        # Memgraph addresses constraints by pattern, so the name is
        # irrelevant here.
        del discovered_name
        return f"DROP CONSTRAINT ON (n:{label}) ASSERT n.{prop} IS UNIQUE;"

    def create_index(self, label: str, prop: str) -> str:
        return f"CREATE INDEX ON :{label}({prop});"

    def show_constraints(self) -> str:
        return "SHOW CONSTRAINT INFO;"

    def constraint_row_matches(self, row: ResultRow, label: str, prop: str) -> bool:
        return row.get("label") == label and row.get("properties") == [prop]

    def constraint_row_name(self, row: ResultRow) -> str | None:
        # Memgraph drops by pattern, so it never needs one.
        del row
        return None

    def apply_memory_limit(self, query: str, mb: int) -> str:
        if CYPHER_MEMORY_LIMIT_TOKEN in query.upper():
            return query
        stripped = query.rstrip()
        if stripped.endswith(CYPHER_SEMICOLON):
            stripped = stripped[: -len(CYPHER_SEMICOLON)].rstrip()
        suffix = CYPHER_MEMORY_LIMIT_SUFFIX.format(mb=mb)
        return f"{stripped}{suffix}{CYPHER_SEMICOLON}"


class Neo4jDialect:
    """Neo4j 5.x.

    Three differences carry real risk rather than being cosmetic:

    * DDL is name-addressed, not pattern-addressed. `DROP CONSTRAINT` in
      particular accepts only a name, hence `_constraint_name`.
    * `SHOW CONSTRAINTS` returns `labelsOrTypes` as a LIST. Comparing it
      to a bare label silently matches nothing, which would make the
      legacy-key migration a no-op instead of an error.
    * There is no per-query memory clause. Appending Memgraph's would
      make every single read a syntax error, so the limit is dropped
      here; the equivalent control is server-side configuration.
    """

    name = DIALECT_NEO4J

    def create_constraint(self, label: str, prop: str) -> str:
        return (
            f"CREATE CONSTRAINT {_constraint_name(label, prop)} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )

    def drop_constraint(
        self, label: str, prop: str, discovered_name: str | None = None
    ) -> str:
        # Prefer the name the server reported: a legacy constraint was
        # created before this code existed and need not carry the name we
        # would derive, in which case dropping the derived name is a
        # no-op and the obsolete key stays enforced.
        return f"DROP CONSTRAINT {discovered_name or _constraint_name(label, prop)} IF EXISTS"

    def create_index(self, label: str, prop: str) -> str:
        return (
            f"CREATE INDEX {_constraint_name(label, prop)} IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.{prop})"
        )

    def show_constraints(self) -> str:
        return "SHOW CONSTRAINTS"

    def constraint_row_matches(self, row: ResultRow, label: str, prop: str) -> bool:
        labels = row.get("labelsOrTypes")
        if not isinstance(labels, list):
            return False
        return label in labels and row.get("properties") == [prop]

    def constraint_row_name(self, row: ResultRow) -> str | None:
        name = row.get("name")
        return name if isinstance(name, str) and name else None

    def apply_memory_limit(self, query: str, mb: int) -> str:
        # `mb` is part of the protocol every dialect implements; Neo4j has
        # no per-query memory clause, so the bound is deliberately dropped
        # here rather than translated.
        del mb
        return query


_DIALECTS: dict[str, type[GraphDialect]] = {
    DIALECT_MEMGRAPH: MemgraphDialect,
    DIALECT_NEO4J: Neo4jDialect,
}


def available_dialects() -> tuple[str, ...]:
    return tuple(sorted(_DIALECTS))


def get_dialect(name: str) -> GraphDialect:
    """Resolve an engine name to its dialect.

    Unknown names raise rather than falling back to a default: a typo in
    `GRAPH_BACKEND` that silently selected Memgraph would emit
    Memgraph DDL at a Neo4j server, and the swallowed-DDL path described
    in `graph_service.ensure_constraints` would hide that until the graph
    was already corrupt.
    """
    try:
        return _DIALECTS[name.strip().lower()]()
    except KeyError:
        raise ValueError(
            f"Unknown graph backend {name!r}; expected one of "
            f"{', '.join(available_dialects())}"
        ) from None
