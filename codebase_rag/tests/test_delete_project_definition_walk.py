"""`CYPHER_DELETE_PROJECT` reaches every node a module owns (issue #1806).

The project delete walks containment to each container, then ONE
variable-length walk from the container to what it defines. A second
`OPTIONAL MATCH ... ->(defined)` cannot widen that: `defined` is already
bound, so the second clause only re-checks the first one's rows and never
adds a node. That is how a merge left the Constant out -- `HAS_VARIANT` and
`DEFINES_CONSTANT` each landed on a clause of their own, and deleting a
project left its Constant nodes behind.

The evals emulator does not model this query, so the check is on its shape,
derived from `CYPHER_DELETE_MODULE` (which the emulator IS checked against)
rather than restated: a restated list is how the two drift.
"""

from __future__ import annotations

import re

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq

_DEFINITION_WALK = re.compile(r"OPTIONAL MATCH \(container\)-\[:([A-Z_|]+)\*\]")

# Queries that take "what a project owns" to be what deleting it removes: a
# project's export and its `cgr stats` counts.
_OWNERSHIP_QUERIES = {
    "export_nodes": cq.CYPHER_EXPORT_PROJECT_NODES,
    "export_relationships": cq.CYPHER_EXPORT_PROJECT_RELATIONSHIPS,
    "stats_node_counts": cq.CYPHER_STATS_PROJECT_NODE_COUNTS,
    "stats_relationship_counts": cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS,
    "stats_per_project": cq.CYPHER_STATS_PER_PROJECT,
}
_DEFINITION_WALKERS = {
    "delete_project": cq.CYPHER_DELETE_PROJECT,
    "delete_project_if_root": cq.CYPHER_DELETE_PROJECT_IF_ROOT,
} | _OWNERSHIP_QUERIES


def _walked(query: str) -> frozenset[str]:
    (walk,) = _DEFINITION_WALK.findall(query)
    return frozenset(walk.split("|"))


def _module_walk() -> frozenset[str]:
    match = re.search(r"OPTIONAL MATCH \(m\)-\[:([A-Z_|]+)\*", cs.CYPHER_DELETE_MODULE)
    assert match is not None, "CYPHER_DELETE_MODULE no longer holds its walk"
    return frozenset(match.group(1).split("|"))


def test_the_project_delete_has_one_definition_walk() -> None:
    walks = _DEFINITION_WALK.findall(cq.CYPHER_DELETE_PROJECT)
    assert len(walks) == 1, walks


def test_the_project_delete_walks_everything_a_module_delete_does() -> None:
    (walk,) = _DEFINITION_WALK.findall(cq.CYPHER_DELETE_PROJECT)
    walked = frozenset(walk.split("|"))
    # A Section hangs off its Module by CONTAINS_SECTION, which the project
    # delete follows in its containment walk instead.
    expected = _module_walk() - {cs.RelationshipType.CONTAINS_SECTION.value}
    assert expected <= walked, sorted(expected - walked)
    assert cs.RelationshipType.DEFINES_CONSTANT.value in walked
    assert cs.RelationshipType.HAS_VARIANT.value in walked


@pytest.mark.parametrize(
    "query", _OWNERSHIP_QUERIES.values(), ids=_OWNERSHIP_QUERIES.keys()
)
def test_a_project_owns_exactly_what_its_delete_removes(query: str) -> None:
    # Each of these restates the delete's relation list, so a relation added
    # to the delete alone leaves its nodes out of the export and the counts.
    assert _walked(query) == _walked(cq.CYPHER_DELETE_PROJECT)


@pytest.mark.parametrize(
    "query", _DEFINITION_WALKERS.values(), ids=_DEFINITION_WALKERS.keys()
)
def test_no_ownership_walk_follows_a_type_reference(query: str) -> None:
    # A Constant's OF_TYPE edge can reach a class another project defines;
    # following it would delete, export and count that class as this one's.
    assert cs.RelationshipType.OF_TYPE.value not in _walked(query)
