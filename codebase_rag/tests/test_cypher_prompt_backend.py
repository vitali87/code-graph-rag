"""The Cypher-generation prompt must match the configured backend (issue #1813).

Sections 2b/2c hard-coded Memgraph's MAGE catalogue (`nxalg.*`, `wcc.*`,
`pagerank.*`, `path.expand`, ...). None of those exist in Neo4j 5, which
#1590 added support for: the nearest equivalents are GDS (a different call
shape, needing a projected graph) and APOC, both separately installed plugins
that cannot be assumed present. A Neo4j deployment was being told to call
procedures that are not there.

Only query GENERATION is affected -- indexing works on both engines.
"""

from __future__ import annotations

import re

import pytest

from codebase_rag import prompts
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, DIALECT_NEO4J

# The catalogue RECOMMENDS a procedure as `CALL <name>(...)`. The Neo4j
# section also NAMES the MAGE families in its do-not-use warning, so a bare
# "is 'nxalg' in the text" check cannot tell a recommendation from a warning
# -- it reports the fixed prompt as broken. Match the call form instead.
_CALL_RE = re.compile(r"CALL\s+([A-Za-z_][\w.]*)\s*\(")


def _recommended_procedures(backend: str) -> set[str]:
    return set(_CALL_RE.findall(prompts.build_cypher_query_rules(backend)))


def test_neo4j_prompt_recommends_no_graph_procedures() -> None:
    """The defect: MAGE procedures offered to an engine that has none."""
    assert _recommended_procedures(DIALECT_NEO4J) == set()


def test_memgraph_prompt_keeps_the_mage_catalogue() -> None:
    """The control. Asserting only the Neo4j side would pass just as well if
    the catalogue were deleted for everyone, which would be a regression for
    the default backend rather than a fix."""
    procedures = _recommended_procedures(DIALECT_MEMGRAPH)
    assert "nxalg.strongly_connected_components" in procedures
    assert "pagerank.get" in procedures
    assert "path.expand" in procedures
    # The catalogue is ~23 entries; a couple surviving would mean it was
    # gutted rather than kept.
    assert len(procedures) > 15


def test_neo4j_prompt_offers_builtin_alternatives() -> None:
    """Removing the catalogue must not leave algorithmic questions unanswered:
    the Neo4j section has to say what to use instead, or the model falls back
    to the unbounded paths section 2 forbids."""
    rules = prompts.build_cypher_query_rules(DIALECT_NEO4J)
    assert "shortestPath(" in rules
    assert "[:CALLS*1..6]" in rules
    assert "MAGE catalogue" in rules, "must say why, not just omit"


def test_neo4j_prompt_prefers_the_gql_exists_spelling() -> None:
    """The Neo4j section must offer `EXISTS { ... }` and must not justify it
    with a false removal claim.

    An earlier version of this test asserted that Neo4j 5 "removed" the
    `exists(<pattern>)` function form. It did not: `exists(input)` is a
    current predicate function in the v5 manual, documented with
    `exists((p)-[:ACTED_IN]->())` as its own example. What v5 replaced is the
    PROPERTY overload `exists(prop)`, by `prop IS NOT NULL` -- a different
    thing. Shipping a wrong "X was removed" to the model is the same defect
    class as the MAGE catalogue this split exists to stop serving, so the
    prompt states a preference and this test holds it to that.
    """
    rules = prompts.build_cypher_query_rules(DIALECT_NEO4J)
    assert "EXISTS { (a)-[:CALLS]->(b) }" in rules
    assert "(a)-[:CALLS]->(b)" in rules  # the bare pattern predicate
    # No removal/deprecation claim about exists() may reach the model.
    for match in re.finditer(r"exists", rules, re.IGNORECASE):
        window = rules[max(0, match.start() - 120) : match.end() + 120]
        assert "removed" not in window.lower(), (
            f"prompt claims exists() was removed: {window!r}"
        )
        assert "deprecated" not in window.lower(), (
            f"prompt claims exists() was deprecated: {window!r}"
        )


def test_only_memgraph_gets_the_mage_pointer_in_section_two() -> None:
    """Section 2's unbounded-paths bullet pointed at 'a MAGE procedure (see
    Section 2b)'. On Neo4j that names a section that no longer offers one."""
    assert "use a MAGE procedure" in prompts.build_cypher_query_rules(DIALECT_MEMGRAPH)
    assert "use a MAGE procedure" not in prompts.build_cypher_query_rules(DIALECT_NEO4J)


@pytest.mark.parametrize(
    ("backend", "engine"), [(DIALECT_MEMGRAPH, "Memgraph"), (DIALECT_NEO4J, "Neo4j")]
)
def test_prompt_names_the_configured_engine(backend: str, engine: str) -> None:
    """The preamble called the store 'a Memgraph knowledge graph' regardless
    of backend."""
    assert f"**{engine} knowledge graph**" in prompts.build_graph_schema_and_rules(
        backend
    )


@pytest.mark.parametrize("backend", [DIALECT_MEMGRAPH, DIALECT_NEO4J])
def test_shared_rules_survive_on_both_backends(backend: str) -> None:
    """Section 2 is engine-neutral and must not be lost by the split."""
    rules = prompts.build_cypher_query_rules(backend)
    assert "**2. Critical Cypher Query Rules**" in rules
    assert "NEVER use unbounded variable-length paths" in rules
    assert "ALWAYS Return Specific Properties with Aliases" in rules
    # No placeholder may reach the model.
    assert prompts._UNBOUNDED_REACHABILITY_SLOT not in rules


@pytest.mark.parametrize("backend", [DIALECT_MEMGRAPH, DIALECT_NEO4J])
def test_literal_braces_survive_the_substitution(backend: str) -> None:
    """The reason this split was backed out of #1590: the rules text contains
    literal braces (`{name: 'VatManager'}`, `EXISTS { ... }`) that str.format
    reads as replacement fields and rejects. The substitution must be a plain
    replace, and these examples must reach the model intact."""
    full = prompts.build_graph_schema_and_rules(backend)
    assert "{name: 'VatManager'}" in full


def test_backend_is_read_at_call_time_not_import_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt frozen at import keeps advertising the engine that was
    configured when the module first loaded. `prompts` is imported long before
    a test or embedding host can set GRAPH_BACKEND."""
    monkeypatch.setattr(prompts.settings, "GRAPH_BACKEND", DIALECT_NEO4J)
    assert _recommended_procedures(None) == set()  # type: ignore[arg-type]

    monkeypatch.setattr(prompts.settings, "GRAPH_BACKEND", DIALECT_MEMGRAPH)
    assert "nxalg.simple_cycles" in _recommended_procedures(None)  # type: ignore[arg-type]


def test_generated_neo4j_prompt_passes_the_read_only_guard() -> None:
    """End-to-end: no Cypher the Neo4j prompt suggests may be rejected by the
    procedure allowlist. The allowlist is deliberately NOT widened to admit
    `gds.`/`apoc.` -- the prompt recommends no procedures on Neo4j, so the
    mismatch the issue describes is unreachable rather than merely tolerated,
    and loosening a security control to permit calls nothing suggests would
    be the wrong trade."""
    from codebase_rag.services.llm import _validate_call_procedures

    rules = prompts.build_cypher_query_rules(DIALECT_NEO4J)
    fragments = [
        frag
        for frag in re.findall(r"`([^`]+)`", rules)
        if re.search(r"\b(MATCH|CALL|WHERE|RETURN)\b", frag)
    ]
    assert fragments, "fixture found no Cypher; the loop below would be vacuous"
    for fragment in fragments:
        _validate_call_procedures(fragment)

    # Known-positive: a zero above means "nothing rejected" only if the guard
    # can reject at all on the same run.
    with pytest.raises(Exception):
        _validate_call_procedures("CALL gds.pageRank.stream('g') YIELD nodeId")


def test_the_neo4j_ranking_example_restricts_every_aggregated_alias() -> None:
    """An aggregate example must scope BOTH ends of the relationship.

    Raised by Greptile on #1839. The finding as filed attributed the refusal
    to the pattern predicates this section recommends, which does not hold --
    varying only the predicate never changes `requires_project_evidence`'s
    verdict, and its own property-comparison control is refused too. Scoped
    aggregates are refused on `main` regardless, by design (#1494: a count
    exposes a MAGNITUDE spanning every indexed project).

    What DID hold is smaller and real: the example named an unrestricted
    caller alias, so its total would span projects the caller never asked
    about. That is a bad example independently of any validator, since the
    prompt teaches the shape the model then emits.
    """
    rules = prompts.build_cypher_query_rules(DIALECT_NEO4J)
    ranking = next(
        frag for frag in re.findall(r"`([^`]+)`", rules) if "count(r)" in frag
    )
    # Every alias the aggregate ranges over is prefix-restricted, not only
    # the projected one.
    aliases = set(re.findall(r"\((\w+):Function\)", ranking))
    assert aliases, "fixture matched no aliases; the loop below would be vacuous"
    for alias in aliases:
        assert f"{alias}.qualified_name STARTS WITH" in ranking, (
            f"alias {alias!r} is aggregated but never restricted: {ranking}"
        )
