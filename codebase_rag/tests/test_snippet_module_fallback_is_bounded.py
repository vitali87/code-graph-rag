"""The snippet lookup's Module fallback walks containment only (issue #2196).

`CYPHER_FIND_BY_QUALIFIED_NAME` fell back to `OPTIONAL MATCH (m:Module)-[*]-(n)`
for a node without its own `path`. With no type, direction or length bound the
expansion ran through CALLS, IMPORTS and every other edge before `LIMIT 1`
applied, so its cost grew with the whole project's connectivity: on a real
repository a lookup for a node with hundreds of callers exhausted Memgraph's
4 GiB query memory.

The defining Module is reachable through the containment edges alone, walked
towards the node. These tests pin that shape from the query text and tie the
edge set to `RELATIONSHIP_SCHEMAS`, so a new snippet label that gains a new
containment edge cannot silently lose its fallback.
"""

from __future__ import annotations

import re

from codebase_rag.constants import SNIPPET_NODE_LABELS, NodeLabel
from codebase_rag.cypher_queries import (
    CYPHER_FIND_BY_QUALIFIED_NAME,
    SNIPPET_CONTAINMENT_RELATIONSHIPS,
    SNIPPET_MODULE_FALLBACK_MAX_DEPTH,
)
from codebase_rag.types_defs import RELATIONSHIP_SCHEMAS

_FALLBACK = re.compile(
    r"OPTIONAL\s+MATCH\s+\(m:Module\)"
    r"-\[:(?P<types>[A-Z_|]+)\*(?P<low>\d+)\.\.(?P<high>\d+)\]->\(n\)"
)


def test_the_fallback_follows_only_containment_edges_towards_the_node() -> None:
    match = _FALLBACK.search(CYPHER_FIND_BY_QUALIFIED_NAME)

    assert match, (
        "no typed, bounded, directed Module fallback in:\n"
        f"{CYPHER_FIND_BY_QUALIFIED_NAME}"
    )
    assert set(match["types"].split("|")) == {
        rel.value for rel in SNIPPET_CONTAINMENT_RELATIONSHIPS
    }
    assert int(match["low"]) == 1
    assert int(match["high"]) == SNIPPET_MODULE_FALLBACK_MAX_DEPTH


def test_the_query_has_no_untyped_variable_length_pattern() -> None:
    """`-[*]-`, `-[*..5]-` and `-[r*]-` all expand over every edge type."""
    untyped = re.findall(r"\[\w*\*", CYPHER_FIND_BY_QUALIFIED_NAME)

    assert untyped == [], CYPHER_FIND_BY_QUALIFIED_NAME


def test_the_untyped_detector_sees_the_shapes_it_refuses() -> None:
    """The check above passes vacuously if its pattern cannot see the defect."""
    for shape in ("(m)-[*]-(n)", "(m)-[*..5]-(n)", "(m)-[r*]-(n)"):
        assert re.findall(r"\[\w*\*", shape), shape


def test_the_containment_set_matches_the_schema() -> None:
    """Every edge in the set is declared as ending on a snippet label, so none
    is dead weight in the pattern."""
    declared = {
        schema.rel_type
        for schema in RELATIONSHIP_SCHEMAS
        if set(schema.targets) & SNIPPET_NODE_LABELS
    }

    assert set(SNIPPET_CONTAINMENT_RELATIONSHIPS) <= declared


def test_every_pathless_snippet_label_is_reachable_by_containment() -> None:
    """A Module carries its own path. Every other snippet label must be the
    target of some containment edge, or its fallback can never bind."""
    reachable = {
        target
        for schema in RELATIONSHIP_SCHEMAS
        if schema.rel_type in SNIPPET_CONTAINMENT_RELATIONSHIPS
        for target in schema.targets
    }

    missing = SNIPPET_NODE_LABELS - {NodeLabel.MODULE} - reachable

    assert missing == set(), sorted(label.value for label in missing)
