"""No shipped query mixes an aggregate with a variable it does not group by.

`RETURN x + count(y)` runs on Memgraph but is an "ambiguous aggregation"
error on Neo4j 5 unless `x` is also returned as a grouping key of its own;
the endpoint and dead-code queries shipped that way. Each query is parsed
with the embedded engine's Cypher parser and every aggregating projection
checked.
"""

from __future__ import annotations

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import EG_AGGREGATES
from codebase_rag.embedded_graph import cypher_ast as a
from codebase_rag.embedded_graph.expressions import children, free_vars, is_aggregate
from codebase_rag.embedded_graph.parser import parse

# Pieces the graph audit formats into a query, not queries themselves.
_FRAGMENTS = frozenset(
    {"CYPHER_AUDIT_IS_NULL", "CYPHER_AUDIT_MISSING_REQUIRED", "CYPHER_AUDIT_OR"}
)
_QUERY_NAMES = sorted(
    n
    for n in dir(cq)
    if n.startswith("CYPHER_")
    and isinstance(getattr(cq, n), str)
    and n not in _FRAGMENTS
)


def _outside_aggregates(e: a.Expr) -> set[str]:
    """Variables an expression reads other than inside an aggregate call."""
    if isinstance(e, a.FuncCall) and e.name in EG_AGGREGATES:
        return set()
    if isinstance(e, a.Var):
        return {e.name}
    if isinstance(e, (a.ListComp, a.Quantifier)):
        return free_vars(e)
    out: set[str] = set()
    for child in children(e):
        out |= _outside_aggregates(child)
    return out


def ambiguous_aggregates(proj: a.Projection) -> list[str]:
    """Aliases of items that aggregate and also read an ungrouped variable."""
    keys = {
        item.expr.name
        for item in proj.items
        if isinstance(item.expr, a.Var) and not is_aggregate(item.expr)
    }
    return [
        item.alias
        for item in proj.items
        if is_aggregate(item.expr) and _outside_aggregates(item.expr) - keys
    ]


@pytest.mark.parametrize("name", _QUERY_NAMES)
def test_no_aggregate_is_mixed_with_an_ungrouped_variable(name: str) -> None:
    query = parse(getattr(cq, name))
    assert isinstance(query, a.Query)
    for part in query.parts:
        for clause in part.clauses:
            if isinstance(clause, a.Projection):
                assert ambiguous_aggregates(clause) == []


def test_the_check_flags_the_shape_neo4j_rejects() -> None:
    query = parse(
        "MATCH (h)-[:EXPOSES]->(e) WITH h, e, count(*) AS resolved "
        "OPTIONAL MATCH (d)-[:READS_FROM]->(e) "
        "RETURN h.name AS handler, resolved + count(DISTINCT d) AS callers"
    )
    assert isinstance(query, a.Query)
    projection = query.parts[0].clauses[-1]
    assert isinstance(projection, a.Projection)
    assert ambiguous_aggregates(projection) == ["callers"]
