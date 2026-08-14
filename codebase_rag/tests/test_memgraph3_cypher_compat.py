# Memgraph 3.x rejects pattern expressions inside WHERE clauses
# (`WHERE NOT (m)<--()` fails with "Not yet implemented: atom expression"),
# which crashed `cgr start --update-graph` post-sync (issue #1257). Every
# shipped Cypher constant must avoid the pattern-in-WHERE shape; the
# OPTIONAL MATCH + count rewrite is accepted by Memgraph 2.x and 3.x alike.

from __future__ import annotations

import re

import pytest

from codebase_rag import constants, cypher_queries

# A WHERE predicate ends at the next clause keyword (or the query's end).
_WHERE_SPLIT = re.compile(r"\bWHERE\b")
_CLAUSE_BOUNDARY = re.compile(
    r"\b(RETURN|WITH|MATCH|OPTIONAL|UNWIND|MERGE|DETACH|DELETE|SET|REMOVE"
    r"|CREATE|ORDER|SKIP|LIMIT|CALL|UNION)\b"
)
# A relationship pattern inside a predicate: a closing node paren joined to an
# opening one by an edge (`--`, `<--`, `-->`, or a bracketed relationship).
_RELATIONSHIP_IN_PREDICATE = re.compile(r"\)\s*<?-\s*(?:\[[^\]]*\]\s*)?->?\s*\(")


def _where_predicates(query: str) -> list[str]:
    predicates = []
    for tail in _WHERE_SPLIT.split(query)[1:]:
        boundary = _CLAUSE_BOUNDARY.search(tail)
        predicates.append(tail[: boundary.start()] if boundary else tail)
    return predicates


def _pattern_predicates(query: str) -> list[str]:
    return [
        predicate.strip()
        for predicate in _where_predicates(query)
        if _RELATIONSHIP_IN_PREDICATE.search(predicate)
    ]


def _string_constants(module) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str)
    }


@pytest.mark.parametrize(
    "query",
    [
        # The two shapes that crashed on Memgraph 3.3.0 before the rewrite.
        "MATCH (m:ExternalModule) WHERE NOT (m)<--() DETACH DELETE m",
        "MATCH (n) WHERE NOT n:Project AND NOT (n)--() RETURN labels(n)[0]",
        "MATCH (n) WHERE NOT (n)-[:REL]->() RETURN n",
        "MATCH (n) WHERE (n)<-[:DEFINES]-(:Module) RETURN n",
    ],
)
def test_detector_flags_pattern_predicates(query: str) -> None:
    assert _pattern_predicates(query)


@pytest.mark.parametrize(
    "query",
    [
        # Patterns are legal in MATCH clauses; only WHERE predicates matter.
        "MATCH ()-->(n) WHERE n.x = 0 RETURN n",
        "MATCH (n) OPTIONAL MATCH (n)--(x) WITH n, count(x) AS degree "
        "WHERE degree = 0 RETURN n",
        "MATCH (a)-[r:CALLS]->(b) WHERE coalesce(r.static_missed, false) = false "
        "RETURN a",
        "MATCH (n) WHERE n.qualified_name STARTS WITH $prefix RETURN n",
    ],
)
def test_detector_permits_legal_queries(query: str) -> None:
    assert _pattern_predicates(query) == []


def test_no_pattern_expressions_in_where_clauses():
    offenders = []
    for module in (cypher_queries, constants):
        for name, value in _string_constants(module).items():
            for predicate in _pattern_predicates(value):
                offenders.append((module.__name__, name, predicate))
    assert not offenders, offenders


def test_orphan_cleanup_queries_use_portable_rewrite():
    delete_orphans = constants.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES
    assert "OPTIONAL MATCH" in delete_orphans
    assert "count(x) AS inbound" in delete_orphans
    assert "WHERE inbound = 0" in delete_orphans

    audit_orphans = cypher_queries.CYPHER_AUDIT_ORPHANS
    assert "OPTIONAL MATCH" in audit_orphans
    assert "count(x) AS degree" in audit_orphans
    assert "WHERE degree = 0" in audit_orphans
