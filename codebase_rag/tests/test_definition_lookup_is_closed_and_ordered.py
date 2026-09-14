"""Definition lookups match a closed label set and pick deterministically.

Issue #1925. Identity constraints are label-scoped, so two nodes may
legitimately share one `qualified_name` -- a Python `@property` backed by
`self.x` emits both a `Field` and a `Method` at `<cls>.x`. A `Field` row
carries no `end_line`, so when one won a definition lookup the caller's own
validation rejected it and reported an indexed definition as not found.

The first fix excluded `Field` and `Parameter` by name. That closes the
instance but fails OPEN: the next property-bearing label added starts
winning definition lookups again, silently. These tests pin the two
properties that make the failure impossible rather than merely absent:

* the lookups match an ALLOWLIST, so an unknown label cannot be returned
  no matter when it was added;
* the pick among equals is ordered, so it does not depend on storage order.
"""

from __future__ import annotations

import re

import pytest

from codebase_rag import constants as cs
from codebase_rag.cypher_queries import (
    CYPHER_FIND_BY_QUALIFIED_NAME,
    CYPHER_GLOSS_TARGET,
    CYPHER_GRAPH_DEFINITION,
)

# Every lookup that resolves one qualified name to a single definition row.
_DEFINITION_LOOKUPS = {
    "find_by_qualified_name": CYPHER_FIND_BY_QUALIFIED_NAME,
    "graph_definition": CYPHER_GRAPH_DEFINITION,
    "gloss_target": CYPHER_GLOSS_TARGET,
}

# Labels that carry no end_line, so a definition lookup returning one makes a
# real definition read as not found. Field and Parameter exist today; the list
# is what a future property-bearing label joins.
_PROPERTY_BEARING = (cs.NodeLabel.FIELD, cs.NodeLabel.PARAMETER)


def _matched_labels(query: str) -> set[str]:
    """The labels the query's MATCH admits, from `(n:A|B|C)`."""
    match = re.search(r"MATCH \(n:([A-Za-z|]+)\)", query)
    assert match is not None, f"no label-scoped MATCH found in:\n{query}"
    return set(match.group(1).split("|"))


@pytest.mark.parametrize("name", sorted(_DEFINITION_LOOKUPS))
def test_a_definition_lookup_matches_the_shared_allowlist(name: str) -> None:
    """Not merely 'excludes Field': the MATCH is closed over one named set,
    so a label nobody has written yet cannot be returned either."""
    labels = _matched_labels(_DEFINITION_LOOKUPS[name])
    expected = {label.value for label in cs.DEFINITION_NODE_LABELS}
    assert labels == expected, (name, sorted(labels), sorted(expected))


@pytest.mark.parametrize("name", sorted(_DEFINITION_LOOKUPS))
def test_a_definition_lookup_orders_before_limiting(name: str) -> None:
    """`LIMIT 1` over a label-ambiguous match without an ORDER BY picks by
    storage order, so the same graph can answer differently across a
    re-index."""
    query = _DEFINITION_LOOKUPS[name]
    assert "LIMIT 1" in query, query
    order_at = query.find("ORDER BY")
    assert order_at != -1, f"{name} limits without ordering:\n{query}"
    assert order_at < query.find("LIMIT 1"), f"{name} orders after LIMIT:\n{query}"


@pytest.mark.parametrize("name", sorted(_DEFINITION_LOOKUPS))
def test_the_ordering_is_total_on_a_colliding_name(name: str) -> None:
    """`qualified_name` ties by definition in exactly the ambiguous case, so
    ordering on it alone would leave the pick arbitrary. The key must break
    the tie on something that differs between the colliding rows."""
    query = _DEFINITION_LOOKUPS[name]
    order_clause = query[query.find("ORDER BY") :].splitlines()[0]
    assert "labels(n)" in order_clause, (name, order_clause)


@pytest.mark.parametrize("label", _PROPERTY_BEARING)
def test_a_property_bearing_label_is_not_a_definition(label: cs.NodeLabel) -> None:
    """The labels whose rows have no end_line stay out of the set every
    definition lookup is built from."""
    assert label not in cs.DEFINITION_NODE_LABELS, label


@pytest.mark.parametrize("name", sorted(_DEFINITION_LOOKUPS))
def test_no_lookup_names_a_label_it_excludes(name: str) -> None:
    """The regression guard. A `NOT n:Field` style exclusion reintroduces the
    fail-open denylist even while the tests above still pass, because a
    denylist can sit alongside an allowlist and quietly become the thing
    maintained."""
    query = _DEFINITION_LOOKUPS[name]
    assert "NOT n:" not in query, f"{name} excludes labels by name:\n{query}"
