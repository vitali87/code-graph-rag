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
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.cypher_queries import (
    CYPHER_FIND_BY_QUALIFIED_NAME,
    CYPHER_GLOSS_TARGET,
    CYPHER_GRAPH_DEFINITION,
)
from codebase_rag.schema_parse import parsed_node_schemas
from codebase_rag.tools.code_retrieval import CodeRetriever

# The scalar type names the schema grammar uses, as `schema_parse` reports
# them in `PropertySpec.type_name`. Spelled here rather than imported because
# `schema_parse` keeps its vocabulary private, and a test naming the literal
# is what catches a rename of the declared type itself.
_SCHEMA_TYPE_STRING = "string"
_SCHEMA_TYPE_INT = "int"

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


# The snippet lookup reads source, so it admits the definition labels plus any
# other node carrying a readable span; the graph-query and gloss lookups admit
# definitions only. Both are named sets, which is the point.
_LOOKUP_LABEL_SETS = {
    "find_by_qualified_name": "SNIPPET_NODE_LABELS",
    "graph_definition": "DEFINITION_NODE_LABELS",
    "gloss_target": "DEFINITION_NODE_LABELS",
}


@pytest.mark.parametrize("name", sorted(_DEFINITION_LOOKUPS))
def test_a_definition_lookup_matches_a_shared_allowlist(name: str) -> None:
    """Not merely 'excludes Field': the MATCH is closed over one named set,
    so a label nobody has written yet cannot be returned either."""
    labels = _matched_labels(_DEFINITION_LOOKUPS[name])
    expected = {label.value for label in getattr(cs, _LOOKUP_LABEL_SETS[name])}
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


# Every label whose rows carry a readable span. Asserting the MATCH equals a
# named set is true by construction for ANY set, including one missing a label
# that used to resolve -- which is how narrowing this lookup silently broke
# Markdown headings. These tests go through the real retriever instead.
_READABLE = {
    cs.NodeLabel.METHOD: "proj.mod.Cls.run",
    cs.NodeLabel.SECTION: "proj.README.Install",
    cs.NodeLabel.PATTERN: "proj.mod.12.4.singleton",
    cs.NodeLabel.CODE_SMELL: "proj.mod.30.0.long-method",
    cs.NodeLabel.SECURITY_ISSUE: "proj.mod.7.2.sql-injection",
}


@pytest.mark.parametrize("label", sorted(_READABLE, key=lambda x: x.value))
@pytest.mark.asyncio
async def test_a_node_with_a_readable_span_is_retrievable(
    tmp_path: Path, label: cs.NodeLabel
) -> None:
    """A row the lookup admits must reach the caller as a snippet. A Section
    carries start_line/end_line/path exactly as a Method does, so dropping it
    from the match reproduced this issue's own symptom (review of #1925)."""
    source = tmp_path / "file.txt"
    source.write_text("one\ntwo\nthree\n")
    admitted = _matched_labels(CYPHER_FIND_BY_QUALIFIED_NAME)

    ingestor = MagicMock()
    # Stand in for the store: return the row only if the query's own MATCH
    # would have admitted this label, so the test tracks the query text.
    row = {
        "name": "x",
        "start": 1,
        "end": 3,
        "path": source.name,
        "absolute_path": str(source),
        "docstring": None,
    }
    ingestor.fetch_all.return_value = [row] if label.value in admitted else []

    retriever = CodeRetriever(str(tmp_path), ingestor)
    result = await retriever.find_code_snippet(_READABLE[label])

    assert result.found, (
        f"{label.value} carries a readable span but the lookup does not admit "
        f"it; admitted={sorted(admitted)}"
    )
    assert result.source_code


@pytest.mark.parametrize(
    "label", sorted(cs.SPAN_BEARING_NODE_LABELS, key=lambda x: x.value)
)
def test_a_span_bearing_label_reaches_the_snippet_lookup(label: cs.NodeLabel) -> None:
    """Every node carrying start_line/end_line/path is retrievable.

    These are not definitions, so they stay out of DEFINITION_NODE_LABELS,
    but `find_code_snippet` can read source for them and did before this
    lookup was narrowed. Dropping one reproduces this issue's own symptom on
    another label, which is how Section and then the three finding nodes were
    each lost in turn (greptile-local, then Greptile on the PR).
    """
    assert label in cs.SNIPPET_NODE_LABELS, label
    assert label not in cs.DEFINITION_NODE_LABELS, label
    assert label.value in _matched_labels(CYPHER_FIND_BY_QUALIFIED_NAME), label


def test_the_allowlist_equals_the_schema_labels_that_declare_a_span() -> None:
    """The allowlist is derived from the SCHEMA, not from itself.

    Every other test in this file takes its cases from
    SNIPPET_NODE_LABELS or its two halves, so each one proves the constant
    is consistent with the query and none can fail when the CONSTANT is
    wrong. Add a span-bearing label to the schema and forget
    SPAN_BEARING_NODE_LABELS, and they all stay green while
    find_code_snippet reports missing-location for that label -- issue
    #1925's own symptom, on a new label (issue #1950).

    `NODE_SCHEMAS` is the independent source: it is what the graph is
    actually built to, and it is maintained for its own reasons. Read
    through `parsed_node_schemas()` rather than by substring, because a
    substring test matches any property CONTAINING the key -- and
    `name_start_line` is already in this schema's vocabulary (Function and
    Method declare it), so a label carrying only `name_start_line` and
    `name_end_line`, with no real span, would otherwise be demanded in the
    allowlist. Equality
    rather than a subset, so it fails in both directions -- a label that
    gains a span and is not admitted, and a label kept in the allowlist
    after losing one.

    The predicate is start_line AND end_line AND path, which is what the
    caller actually requires: `find_code_snippet` rejects a row unless
    the path is a non-empty string AND both line numbers are ints with
    end >= start (code_retrieval.py). Checking the lines alone would
    demand a label be admitted that retrieval could not serve, so the
    predicate names the whole contract (Greptile on #1951). `Field` and `Parameter` declare
    `start_line: int?` and no end_line at all, and that is precisely why
    #1925 excluded them: a Field row winning a definition lookup was
    rejected by the caller's own validation for having no end. So the
    two labels this file exists to keep out are kept out BY the predicate
    rather than by an exception list, and a start_line-only predicate
    would readmit them.

    The predicate pairs each name with its declared TYPE, because the
    grammar accepts every scalar and a name alone cannot tell
    `end_line: int?` from `end_line: string?` -- retype one and a
    name-only predicate stays green while retrieval's int check rejects
    every row for that label (CodeRabbit on #1951).

    It deliberately does NOT require the declarations to be mandatory.
    Eight of the twelve admitted labels declare their span as optional
    (`start_line: int?`), including every definition label, because a
    span is absent for a synthesised node rather than never present --
    `find_code_snippet` validates the VALUE on the row it got, which is
    a runtime question the declaration does not answer. Requiring
    `optional is False` here would demand the allowlist shrink to the
    four labels that happen to declare a mandatory span, which is not
    the eligibility contract.
    """
    readable = {
        (cs.KEY_PATH, _SCHEMA_TYPE_STRING),
        (cs.KEY_START_LINE, _SCHEMA_TYPE_INT),
        (cs.KEY_END_LINE, _SCHEMA_TYPE_INT),
    }
    declared = {
        label
        for label, specs in parsed_node_schemas().items()
        if readable <= {(spec.name, spec.type_name) for spec in specs}
    }
    assert declared == cs.SNIPPET_NODE_LABELS, (
        "SNIPPET_NODE_LABELS has drifted from the schema. "
        f"span-bearing but not admitted: {sorted(x.value for x in declared - cs.SNIPPET_NODE_LABELS)}; "
        f"admitted without a declared span: {sorted(x.value for x in cs.SNIPPET_NODE_LABELS - declared)}"
    )
