"""A scoped query's refusal names the check that fired (issue #2197).

`requires_project_evidence` refuses a scoped query for several reasons, and
every refusal used to say the query returns no qualified name. For most of
them that was false. An agent that followed the advice was refused again,
concluded that aggregates cannot be scoped at all, and paged through raw rows
instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tools.codebase_query import (
    create_query_tool,
    requires_project_evidence,
    unscopeable_message,
)

ALPHA = "alpha__aaaa1111"

# The query from the issue. It projects `callee.qualified_name`; what is
# missing is a restriction on `caller`, the entity it counts.
MOST_CALLED = f"""MATCH (caller)-[:CALLS]->(callee:Function|Method)
WHERE callee.qualified_name STARTS WITH '{ALPHA}.'
RETURN callee.qualified_name AS qualified_name, callee.name AS name,
       count(DISTINCT caller) AS caller_count
ORDER BY caller_count DESC
LIMIT 10"""

REFUSED = [
    pytest.param(
        "MATCH (n) WITH n RETURN n.qualified_name AS qualified_name",
        cs.ScopeRefusal.UNANALYSABLE,
        ("WITH",),
        id="unanalysable",
    ),
    pytest.param(
        "MATCH (n:Function) WHERE n.name = 'x' DETACH DELETE n",
        cs.ScopeRefusal.NO_RETURN,
        (),
        id="no-return",
    ),
    pytest.param(
        "MATCH (n) RETURN left(n.qualified_name, 3) AS head",
        cs.ScopeRefusal.TRANSFORMED_TERM,
        (),
        id="transformed-term",
    ),
    pytest.param(
        f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
        "RETURN n.qualified_name AS qualified_name, count(*) AS total",
        cs.ScopeRefusal.UNBOUND_AGGREGATE,
        (),
        id="unbound-aggregate",
    ),
    pytest.param(
        MOST_CALLED,
        cs.ScopeRefusal.UNRESTRICTED_AGGREGATE,
        ("caller",),
        id="unrestricted-aggregate",
    ),
    pytest.param(
        "MATCH (a)-[:CALLS]->(b) "
        f"WHERE a.qualified_name STARTS WITH '{ALPHA}.' "
        "RETURN count(a) AS callers, count(b) AS callees",
        cs.ScopeRefusal.UNRESTRICTED_AGGREGATE,
        ("b",),
        id="one-of-two-aggregates-unrestricted",
    ),
    pytest.param(
        "MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name AS caller, b.name AS callee",
        cs.ScopeRefusal.UNATTRIBUTED_ENTITY,
        ("b",),
        id="unattributed-entity",
    ),
    pytest.param(
        "MATCH (n:Function) RETURN n.name AS name, n.path AS path",
        cs.ScopeRefusal.NO_QUALIFIED_NAME,
        (),
        id="no-qualified-name",
    ),
]


@pytest.mark.parametrize(("query", "refusal", "subjects"), REFUSED)
def test_the_refusal_says_which_check_fired(
    query: str, refusal: cs.ScopeRefusal, subjects: tuple[str, ...]
) -> None:
    evidence = requires_project_evidence(query, ALPHA)

    assert not evidence
    assert evidence.refusal == refusal
    assert evidence.subjects == subjects


@pytest.mark.parametrize(("query", "refusal", "subjects"), REFUSED)
def test_only_a_query_without_a_qualified_name_is_told_it_has_none(
    query: str, refusal: cs.ScopeRefusal, subjects: tuple[str, ...]
) -> None:
    message = unscopeable_message(requires_project_evidence(query, ALPHA), ALPHA)

    claims_no_qualified_name = "returns no qualified name" in message
    assert claims_no_qualified_name == (refusal == cs.ScopeRefusal.NO_QUALIFIED_NAME)
    for subject in subjects:
        assert f"`{subject}`" in message


def test_the_unrestricted_aggregate_refusal_gives_the_missing_predicate() -> None:
    message = unscopeable_message(requires_project_evidence(MOST_CALLED, ALPHA), ALPHA)

    assert f"`caller.qualified_name STARTS WITH '{ALPHA}.'`" in message


def test_adding_the_predicate_the_refusal_names_makes_the_query_pass() -> None:
    fixed = MOST_CALLED.replace(
        "WHERE callee.qualified_name",
        f"WHERE caller.qualified_name STARTS WITH '{ALPHA}.' AND callee.qualified_name",
    )

    assert requires_project_evidence(fixed, ALPHA)


@pytest.mark.asyncio
async def test_the_scoped_tool_returns_the_named_refusal() -> None:
    cypher_gen = MagicMock()

    async def _generate(_query: str) -> str:
        return MOST_CALLED

    cypher_gen.generate = _generate
    ingestor = MagicMock()

    tool = create_query_tool(ingestor, cypher_gen, project_name=ALPHA)
    result = await tool.function("which functions have the most callers")

    assert result.results == []
    assert result.error == result.summary
    assert "`caller`" in result.summary
    assert "returns no qualified name" not in result.summary
    ingestor.fetch_all.assert_not_called()
