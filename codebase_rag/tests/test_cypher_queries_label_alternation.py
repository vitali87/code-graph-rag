"""The queries cgr runs itself test labels in WHERE, not with `(n:A|B)`.

Label alternation in a node pattern is not openCypher every engine accepts,
and one unsupported clause fails the whole query. On Memgraph 3.3 the WHERE
spelling returned the same rows in the same time (issue #386), and the
embedded graph reads the labels back out of WHERE so a key lookup stays an
index seek; both are checked here. The prompt's example queries are exempt:
they show the model the form its rules ask for.
"""

from __future__ import annotations

import re

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import graph as graph_constants
from codebase_rag.embedded_graph.executor import compile_read, value_type

_ALTERNATION = re.compile(r"\(\s*\w*\s*:\s*\w+(?:\s*\|\s*\w+)+")
_EXEMPT_PREFIX = "CYPHER_EXAMPLE_"


def _runtime_queries() -> list[tuple[str, str]]:
    return sorted(
        (name, text)
        for module in (cq, graph_constants)
        for name, text in vars(module).items()
        if name.startswith("CYPHER_")
        and not name.startswith(_EXEMPT_PREFIX)
        and isinstance(text, str)
    )


@pytest.mark.parametrize(("name", "text"), _runtime_queries())
def test_no_runtime_query_uses_label_alternation(name: str, text: str) -> None:
    assert not _ALTERNATION.search(text), name


def test_the_where_spelling_keeps_the_key_lookup_an_index_seek() -> None:
    plan = compile_read(
        "MATCH (n {qualified_name: $qn}) WHERE (n:Function OR n:Method) "
        "RETURN n.name AS name",
        (("qn", value_type("p.f")),),
        True,
    )
    assert "label IN ('Function', 'Method')" in plan.sql
    assert ".key = " in plan.sql


def test_a_partial_label_test_does_not_narrow_the_node() -> None:
    # `n:Function OR n.x = 1` allows any label, so none may be assumed.
    plan = compile_read(
        "MATCH (n {qualified_name: $qn}) WHERE n:Function OR n.x = 1 "
        "RETURN n.name AS name",
        (("qn", value_type("p.f")),),
        True,
    )
    assert "label IN" not in plan.sql
