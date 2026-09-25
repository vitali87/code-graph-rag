import pytest

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


@pytest.mark.parametrize("key", ["", "1st", "a-b", "a`b", "x y", "n.m"])
def test_a_key_that_is_not_a_plain_identifier_is_refused(key: str) -> None:
    """A property name cannot be a Cypher parameter, so it is spliced into
    the query; anything but an identifier would change the statement."""
    with pytest.raises(ValueError, match="not a property name"):
        build_remove_node_keys_query("File", "path", [key])
