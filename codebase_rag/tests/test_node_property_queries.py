import re

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cypher_queries import (
    build_node_props_query,
    build_remove_node_keys_query,
)


def test_the_props_query_reads_one_node_by_its_key() -> None:
    assert build_node_props_query("File", "path") == (
        "MATCH (n:File {path: $id}) RETURN properties(n) AS props"
    )


def test_the_removal_names_each_key_once_in_sorted_order() -> None:
    query = build_remove_node_keys_query("Function", "qualified_name", {"b", "a_1"})
    assert query == ("MATCH (n:Function {qualified_name: $id}) REMOVE n.`a_1`, n.`b`")


@pytest.mark.parametrize(
    "key", ["", "1st", "a-b", "a`b", "x y", "n.m", "caf\u00e9", "x\u0661"]
)
def test_a_key_that_is_not_a_plain_identifier_is_refused(key: str) -> None:
    """A property name cannot be a Cypher parameter, so it is spliced into
    the query; anything but an identifier would change the statement. Only
    ASCII identifiers pass: a non-ASCII letter or digit is refused too."""
    with pytest.raises(ValueError, match="not a property name"):
        build_remove_node_keys_query("File", "path", [key])


def _subtree_walk(query: str) -> set[str]:
    match = re.search(r"\(\w+\)-\[:([A-Z_|]+)\*0\.\.\]->", query)
    assert match, query
    return set(match.group(1).split("|"))


@pytest.mark.parametrize(
    "query", [cq.CYPHER_CHECK_SCOPE_NODES, cq.CYPHER_CHECK_SCOPE_EDGES]
)
def test_the_isolated_scope_walks_the_module_delete_relations(query: str) -> None:
    """The isolated check captures what the re-ingest's module delete takes,
    so both walk the same relations: a relation only the delete follows
    loses its nodes for good (#1718; HAS_FIELD, then HAS_VARIANT, drifted)."""
    assert _subtree_walk(query) == _subtree_walk(cs.CYPHER_DELETE_MODULE)
