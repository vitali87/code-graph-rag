"""A query the database rejects gets one regeneration fed the engine's error.

Issue #2361: the generator sorted on `labels(n)`, a list, and Memgraph failed
with "Comparison is not defined for values of type list". Guessing at such
queries from their text misses aliases, renames and quoted names; the engine
already says exactly what is wrong, so the query tool hands that back.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import mgclient
import pytest
from pydantic_ai import Tool
from rich.console import Console

from codebase_rag.prompts import build_cypher_repair_request
from codebase_rag.services.graph_service import is_query_rejection
from codebase_rag.services.llm import CypherGenerator
from codebase_rag.tools.codebase_query import create_query_tool

_LIST_SORT_ERROR = "Comparison is not defined for values of type list."
_QUESTION = "what is this repo about?"
_REJECTED = (
    "MATCH (n) WHERE n.qualified_name STARTS WITH 'proj.' "
    "RETURN n.qualified_name AS qualified_name, labels(n) AS type ORDER BY type;"
)
_REPAIRED = _REJECTED.replace("ORDER BY type", "ORDER BY type[0]")
_ROWS = [{"qualified_name": "proj.mod", "type": ["Module"]}]

# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception types are bound once here.
_DatabaseError: type[Exception] = mgclient.DatabaseError  # ty: ignore[unresolved-attribute]
_ProgrammingError: type[Exception] = mgclient.ProgrammingError  # ty: ignore[unresolved-attribute]
_OperationalError: type[Exception] = mgclient.OperationalError  # ty: ignore[unresolved-attribute]


@pytest.mark.parametrize(
    "error",
    [
        _DatabaseError(_LIST_SORT_ERROR),
        _ProgrammingError("syntax error"),
    ],
)
def test_an_engine_refusal_is_a_query_rejection(error: BaseException) -> None:
    assert is_query_rejection(error)


@pytest.mark.parametrize(
    "error",
    [
        _OperationalError("couldn't connect to host: Connection refused"),
        TimeoutError(),
        ValueError("not from the engine"),
    ],
)
def test_other_failures_are_not_query_rejections(error: BaseException) -> None:
    assert not is_query_rejection(error)


def test_neo4j_client_errors_are_rejections_but_access_failures_are_not() -> None:
    exceptions = pytest.importorskip("neo4j.exceptions")
    assert is_query_rejection(exceptions.CypherTypeError())
    assert is_query_rejection(exceptions.CypherSyntaxError())
    assert not is_query_rejection(exceptions.AuthError())
    assert not is_query_rejection(exceptions.TokenExpired())
    assert not is_query_rejection(exceptions.Forbidden())
    assert not is_query_rejection(exceptions.ServiceUnavailable())


def _tool(
    fetch_effects: list[object],
    repaired: str = _REPAIRED,
    project_name: str | None = None,
) -> tuple[Tool, MagicMock, MagicMock]:
    ingestor = MagicMock()
    ingestor.fetch_read_only.side_effect = fetch_effects
    cypher_gen = MagicMock()
    cypher_gen.generate = AsyncMock(return_value=_REJECTED)
    cypher_gen.repair = AsyncMock(return_value=repaired)
    console = Console(file=io.StringIO(), force_terminal=True, width=200)
    tool = create_query_tool(
        ingestor, cypher_gen, console=console, project_name=project_name
    )
    return tool, ingestor, cypher_gen


async def test_a_rejected_query_is_regenerated_once_with_the_error() -> None:
    tool, ingestor, cypher_gen = _tool([_DatabaseError(_LIST_SORT_ERROR), _ROWS])
    result = await tool.function(natural_language_query=_QUESTION)

    cypher_gen.repair.assert_awaited_once_with(_QUESTION, _REJECTED, _LIST_SORT_ERROR)
    assert [call.args[0] for call in ingestor.fetch_read_only.call_args_list] == [
        _REJECTED,
        _REPAIRED,
    ]
    assert result.query_used == _REPAIRED
    assert result.results == _ROWS


async def test_a_lost_connection_is_not_regenerated() -> None:
    tool, _, cypher_gen = _tool([_OperationalError("connection refused")])
    result = await tool.function(natural_language_query=_QUESTION)

    cypher_gen.repair.assert_not_awaited()
    assert result.results == []
    assert "connection refused" in result.summary


async def test_a_second_rejection_is_reported_not_retried_again() -> None:
    tool, ingestor, cypher_gen = _tool(
        [_DatabaseError(_LIST_SORT_ERROR), _DatabaseError("still bad")]
    )
    result = await tool.function(natural_language_query=_QUESTION)

    cypher_gen.repair.assert_awaited_once()
    assert ingestor.fetch_read_only.call_count == 2
    assert result.query_used == _REPAIRED
    assert "still bad" in result.summary


async def test_the_repaired_query_must_still_pass_the_project_scope_check() -> None:
    unscoped = "MATCH (n) RETURN n.name AS name ORDER BY name;"
    tool, ingestor, _ = _tool(
        [_DatabaseError(_LIST_SORT_ERROR)],
        repaired=unscoped,
        project_name="proj",
    )
    result = await tool.function(natural_language_query=_QUESTION)

    assert ingestor.fetch_read_only.call_count == 1
    assert result.query_used == unscoped
    assert result.error


async def test_repair_asks_the_generator_with_the_query_and_error() -> None:
    generator = CypherGenerator.__new__(CypherGenerator)
    with patch.object(
        CypherGenerator, "generate", AsyncMock(return_value=_REPAIRED)
    ) as generate:
        repaired = await generator.repair(_QUESTION, _REJECTED, _LIST_SORT_ERROR)

    assert repaired == _REPAIRED
    assert generate.await_args is not None
    request = generate.await_args.args[0]
    assert request == build_cypher_repair_request(
        _QUESTION, _REJECTED, _LIST_SORT_ERROR
    )
    assert _QUESTION in request
    assert _REJECTED in request
    assert _LIST_SORT_ERROR in request
