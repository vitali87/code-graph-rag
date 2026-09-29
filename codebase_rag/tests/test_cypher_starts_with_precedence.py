"""No Cypher string concatenates onto the right of STARTS/ENDS WITH or CONTAINS.

Memgraph binds those operators tighter than `+`, so `x STARTS WITH name + '.'`
parses as `(x STARTS WITH name) + '.'` and every call raises "Invalid types:
bool and string for '+'". The unit tests run queries against the eval double,
which mirrors them in Python and never parses the Cypher, so such a query
passes every unit test while failing on every real database call.
`CYPHER_UNRESOLVED_IMPORTER_PATHS` shipped that way (issue #1682). This scan
catches the shape without a database: the right operand must be parenthesised.
"""

from __future__ import annotations

import re

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq

_UNPARENTHESISED = re.compile(
    r"\b(?:STARTS WITH|ENDS WITH|CONTAINS)\s+[$\w.]+\s*\+", re.IGNORECASE
)


def _cypher_strings() -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    for module in (cs, cq):
        for name in dir(module):
            value = getattr(module, name)
            if name.startswith("CYPHER_") and isinstance(value, str):
                found[name] = value
    return sorted(found.items())


def test_the_scan_sees_the_query_that_shipped_broken() -> None:
    assert "CYPHER_UNRESOLVED_IMPORTER_PATHS" in dict(_cypher_strings())
    assert _UNPARENTHESISED.search("x STARTS WITH name + '.'")
    assert not _UNPARENTHESISED.search("x STARTS WITH (name + '.')")


@pytest.mark.parametrize(
    "query",
    [query for _, query in _cypher_strings()],
    ids=[name for name, _ in _cypher_strings()],
)
def test_concatenation_after_a_string_operator_is_parenthesised(query: str) -> None:
    assert not _UNPARENTHESISED.search(query), query
