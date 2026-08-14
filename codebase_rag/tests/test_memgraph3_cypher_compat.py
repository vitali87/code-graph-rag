# Memgraph 3.x rejects pattern expressions inside WHERE clauses
# (`WHERE NOT (m)<--()` fails with "Not yet implemented: atom expression"),
# which crashed `cgr start --update-graph` post-sync (issue #1257). Every
# shipped Cypher constant must avoid the pattern-in-WHERE shape; the
# OPTIONAL MATCH + count rewrite is accepted by Memgraph 2.x and 3.x alike.

from __future__ import annotations

from codebase_rag import constants, cypher_queries

# A pattern expression used as a WHERE predicate closes (or opens) with an
# anonymous empty node `()` adjacent to an edge; legitimate MATCH-clause
# patterns bind at least the end they consume.
_FORBIDDEN_FRAGMENTS = (")--()", "<--()", "-->()", "()--(", "()<--", "()-->")


def _string_constants(module) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and isinstance(value, str)
    }


def test_no_pattern_expressions_in_where_clauses():
    offenders = []
    for module in (cypher_queries, constants):
        for name, value in _string_constants(module).items():
            for fragment in _FORBIDDEN_FRAGMENTS:
                if fragment in value:
                    offenders.append((module.__name__, name, fragment))
    assert not offenders, offenders


def test_orphan_cleanup_queries_use_portable_rewrite():
    assert "OPTIONAL MATCH" in constants.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES
    assert "OPTIONAL MATCH" in cypher_queries.CYPHER_AUDIT_ORPHANS
