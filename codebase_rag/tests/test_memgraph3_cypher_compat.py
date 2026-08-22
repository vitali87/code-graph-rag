# Memgraph 3.x rejects pattern expressions inside WHERE clauses
# (`WHERE NOT (m)<--()` fails with "Not yet implemented: atom expression"),
# which crashed `cgr start --update-graph` post-sync (issue #1257). Every
# shipped Cypher constant must avoid the pattern-in-WHERE shape; the
# OPTIONAL MATCH + count rewrite is accepted by Memgraph 2.x and 3.x alike.

from __future__ import annotations

import re
import types

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


# Every constant that is actually sent to a graph database as Cypher is named
# with one of these two shapes by convention: a CYPHER_ prefix (almost all of
# cypher_queries.py and the query constants in constants/graph.py, e.g.
# CYPHER_DELETE_*, CYPHER_ALL_INHERITS) or a _QUERY suffix for the handful
# that live outside that naming scheme (HEALTH_CHECK_MEMGRAPH_QUERY in
# constants/health.py, executed directly at
# tools/health_checker.py:77 via `cursor.execute(cs.HEALTH_CHECK_MEMGRAPH_QUERY)`).
# Verified by enumerating every call site that executes a module-level
# constant as Cypher (`.execute(`, `execute_write(`, `_execute_query(`,
# `fetch_all(` across the non-test tree): every constant reaching one of
# those calls matches one of these two shapes -- see the fix-round-2 report
# for the full enumeration.
#
# constants/*.py also holds plenty of unrelated uppercase strings that
# happen to contain the literal word "WHERE" without being Cypher at all --
# TS_RS_WHERE_CLAUSE and TS_RS_WHERE_PREDICATE are tree-sitter Rust grammar
# node-type names, SHELL_CMD_WHERE is the Windows `where` command, and
# MAGE_PROCEDURE_CATALOG (and later, ARCADE_PROCEDURE_CATALOG) is LLM-prompt
# documentation that shows an example WHERE clause in prose. None of those
# match either shape, so they are excluded automatically -- this is a
# positive rule, not a name-by-name exclusion list, so it does not need
# updating every time a new prose or grammar constant is added.
_CYPHER_QUERY_NAME_PREFIX = "CYPHER_"
_CYPHER_QUERY_NAME_SUFFIX = "_QUERY"


def _string_constants(module) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper()
        and isinstance(value, str)
        and (
            name.startswith(_CYPHER_QUERY_NAME_PREFIX)
            or name.endswith(_CYPHER_QUERY_NAME_SUFFIX)
        )
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


def test_string_constants_selection_is_prefix_based_not_a_denylist() -> None:
    """Pins the scanner's selection mechanism (issue: prose constants like
    MAGE_PROCEDURE_CATALOG false-positived the pattern-in-WHERE detector,
    and a name-only fix would have silently dropped real guard coverage --
    HEALTH_CHECK_MEMGRAPH_QUERY is executed as Cypher but doesn't carry the
    CYPHER_ prefix).

    A future prose/documentation constant must be excluded automatically by
    naming convention alone, with no per-name edit to this test file; a
    future real Cypher query constant -- whether CYPHER_-prefixed or
    _QUERY-suffixed, like HEALTH_CHECK_MEMGRAPH_QUERY -- must still be
    caught by the guard.
    """
    fake_module = types.ModuleType("fake_constants")
    # A documentation-style constant illustrating a WHERE/pattern shape in
    # prose, the same shape MAGE_PROCEDURE_CATALOG has -- must be skipped
    # purely because it matches neither naming shape, not via a name lookup.
    fake_module.SOME_PROCEDURE_CATALOG = (  # type: ignore[attr-defined]
        "follow the procedure call with a WHERE clause that checks "
        "EXISTS((a)-[:CALLS]->(b))"
    )
    # A real Cypher query constant with the same offending shape, named with
    # the CYPHER_ prefix -- must still be selected and catchable.
    fake_module.CYPHER_FAKE_OFFENDER = (  # type: ignore[attr-defined]
        "MATCH (m) WHERE NOT (m)<--() DETACH DELETE m"
    )
    # A real Cypher query constant named with the _QUERY suffix instead of
    # the CYPHER_ prefix, the same shape as the real HEALTH_CHECK_MEMGRAPH_QUERY
    # -- must also still be selected and catchable.
    fake_module.FAKE_HEALTH_QUERY = (  # type: ignore[attr-defined]
        "MATCH (m) WHERE NOT (m)-[:REL]->() RETURN m"
    )

    selected = _string_constants(fake_module)

    assert "SOME_PROCEDURE_CATALOG" not in selected
    assert selected["CYPHER_FAKE_OFFENDER"] == fake_module.CYPHER_FAKE_OFFENDER
    assert selected["FAKE_HEALTH_QUERY"] == fake_module.FAKE_HEALTH_QUERY
    # And the pattern-in-WHERE detector itself still fires on both, so a real
    # regression in either naming shape would still fail the guard test.
    assert _pattern_predicates(selected["CYPHER_FAKE_OFFENDER"])
    assert _pattern_predicates(selected["FAKE_HEALTH_QUERY"])


def test_health_check_query_constant_is_selected_by_the_scanner() -> None:
    """The concrete real-world case the synthetic test above stands in for:
    HEALTH_CHECK_MEMGRAPH_QUERY is executed as Cypher at
    tools/health_checker.py:77 (`cursor.execute(cs.HEALTH_CHECK_MEMGRAPH_QUERY)`)
    but does not carry the CYPHER_ prefix, so it must be reachable only via
    the _QUERY-suffix arm of the selection rule -- and must actually be
    present in `constants` (re-exported from constants/health.py), not just
    theoretically selectable.
    """
    assert hasattr(constants, "HEALTH_CHECK_MEMGRAPH_QUERY")
    selected = _string_constants(constants)
    assert "HEALTH_CHECK_MEMGRAPH_QUERY" in selected


def test_orphan_cleanup_queries_use_portable_rewrite():
    delete_orphans = constants.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES
    assert "OPTIONAL MATCH" in delete_orphans
    assert "count(x) AS inbound" in delete_orphans
    assert "WHERE inbound = 0" in delete_orphans

    audit_orphans = cypher_queries.CYPHER_AUDIT_ORPHANS
    assert "OPTIONAL MATCH" in audit_orphans
    assert "count(x) AS degree" in audit_orphans
    assert "WHERE degree = 0" in audit_orphans
