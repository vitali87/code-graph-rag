# A scoped MCP query must return nothing from other projects (issue #1494).
#
# The graph layer is already multi-project -- `list_projects` enumerates,
# `_get_project_node_ids` scopes by name, and several handlers derive a
# project. The RETRIEVAL handlers did not: `query_code_graph` ran whatever
# Cypher the model generated, unfiltered.
#
# That is why the reported bleed was INTERMITTENT. A code-level filter is
# always-or-never; an instruction the model may or may not follow produces
# exactly "sometimes", which is what was described.
#
# So enforcement is post-execution and keyed on `qualified_name`, which
# begins with the project name for every node the indexer writes. Whatever
# the generated query does or omits, a row from another project cannot
# survive the filter.
from __future__ import annotations

import pytest

# Two projects whose symbols COLLIDE by name. A fixture with distinct names
# passes whether or not scoping works, so it would prove nothing.
ALPHA = "alpha__aaaa1111"
BETA = "beta__bbbb2222"

_ROWS = [
    {"qualified_name": f"{ALPHA}.service.handler", "name": "handler"},
    {"qualified_name": f"{BETA}.service.handler", "name": "handler"},
    {"qualified_name": f"{ALPHA}.models.User", "name": "User"},
    {"qualified_name": f"{BETA}.models.User", "name": "User"},
]


class TestTheFilter:
    """The enforcement primitive, in isolation."""

    def test_rows_from_other_projects_are_dropped(self) -> None:
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        kept = scope_rows_to_project(_ROWS, ALPHA)

        assert [row["qualified_name"] for row in kept] == [
            f"{ALPHA}.service.handler",
            f"{ALPHA}.models.User",
        ]

    def test_the_colliding_name_alone_does_not_decide_it(self) -> None:
        """Both projects declare `handler`; only one row may survive.

        This is the assertion a distinct-names fixture could not make.
        """
        kept = _scoped(ALPHA)
        handlers = [r for r in kept if r["name"] == "handler"]

        assert len(handlers) == 1
        assert handlers[0]["qualified_name"].startswith(f"{ALPHA}.")

    def test_a_prefix_that_is_not_a_component_boundary_does_not_match(self) -> None:
        """`alpha__aaaa1111` must not swallow `alpha__aaaa1111_extra`.

        A bare `startswith(project)` would match a DIFFERENT project whose
        name extends this one. `derive_project_name` appends a digest, so
        such a pair is unlikely -- but "unlikely" is not "impossible", and
        the separator costs nothing to require.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [
            {"qualified_name": f"{ALPHA}.real.Thing"},
            {"qualified_name": f"{ALPHA}_extra.other.Thing"},
        ]

        kept = scope_rows_to_project(rows, ALPHA)

        assert [r["qualified_name"] for r in kept] == [f"{ALPHA}.real.Thing"]

    def test_no_scope_returns_everything(self) -> None:
        """Omitting the scope must preserve current behaviour.

        Every existing caller passes no project, so a filter that defaulted
        to dropping rows would silently empty the CLI.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        assert scope_rows_to_project(_ROWS, None) == _ROWS

    def test_rows_without_a_qualified_name_survive_a_scoped_query(self) -> None:
        """Aggregates carry no qualified_name and must not be discarded.

        `RETURN count(n)` produces a row with no such key. Dropping it would
        turn scoping into silent data loss for every aggregate query --
        a filter that is too aggressive fails as badly as one too lax.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"total": 42}]

        assert scope_rows_to_project(rows, ALPHA) == rows


def _scoped(project: str) -> list[dict]:
    from codebase_rag.tools.codebase_query import scope_rows_to_project

    return scope_rows_to_project(_ROWS, project)


class TestPerRequestScope:
    """One server process must serve many projects.

    A workspace fixed at startup would force one process per project, which
    is the case the issue rules out. So the scope travels with the REQUEST,
    and the handler applies it -- the pre-built `_query_tool` has its
    project bound at construction and cannot vary per call.
    """

    @pytest.mark.asyncio
    async def test_two_requests_can_name_different_projects(self) -> None:
        """The requirement, stated as a test.

        Both calls hit the same handler on the same server object; only the
        argument differs. If the scope were per-process this could not pass.
        """
        handler = _handler_returning(_ROWS)

        alpha = await handler.query_code_graph("everything", project=ALPHA)
        beta = await handler.query_code_graph("everything", project=BETA)

        assert _prefixes(alpha) == {ALPHA}
        assert _prefixes(beta) == {BETA}

    @pytest.mark.asyncio
    async def test_omitting_the_project_returns_every_project(self) -> None:
        """The control, and the backwards-compatibility guarantee.

        Existing clients pass no project. Without this, a handler that
        always scoped to something would satisfy the test above while
        silently narrowing every current caller's results.
        """
        handler = _handler_returning(_ROWS)

        both = await handler.query_code_graph("everything")

        assert _prefixes(both) == {ALPHA, BETA}

    @pytest.mark.asyncio
    async def test_an_unknown_project_is_refused_rather_than_silently_empty(
        self,
    ) -> None:
        """A typo must not look like "this project has no matches".

        Returning zero rows for a misspelled name is indistinguishable from
        a genuine empty result, and the caller cannot tell which happened.
        """
        handler = _handler_returning(_ROWS)

        result = await handler.query_code_graph("everything", project="no-such")

        assert result.get("error")
        assert "no-such" in str(result["error"])


class TestSemanticSearchScope:
    """The other retrieval handler named in the issue.

    Its underlying tool ALREADY accepted `project` and passes it to
    `search_embeddings`, which filters in the vector store. Only the MCP
    handler failed to forward it, so this is a plumbing gap rather than a
    missing filter -- and the fix is correspondingly smaller.
    """

    @pytest.mark.asyncio
    async def test_the_project_reaches_the_underlying_tool(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from codebase_rag.mcp.tools import MCPToolsRegistry

        handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
        handler._ingestor_lock = _NullLock()
        handler._semantic_search_tool = MagicMock()
        handler._semantic_search_tool.function = AsyncMock(return_value="ok")
        handler.ingestor = MagicMock()
        handler.ingestor.list_projects = MagicMock(return_value=[ALPHA, BETA])

        await handler.semantic_search("intent", project=ALPHA)

        kwargs = handler._semantic_search_tool.function.call_args.kwargs
        assert kwargs["project"] == ALPHA

    @pytest.mark.asyncio
    async def test_no_project_is_forwarded_as_none(self) -> None:
        """The control: the default must stay unscoped.

        Asserting the VALUE rather than merely that the key is present --
        forwarding a wrong-but-present project would pass a presence check
        while silently narrowing every unscoped caller.
        """
        from unittest.mock import AsyncMock, MagicMock

        from codebase_rag.mcp.tools import MCPToolsRegistry

        handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
        handler._ingestor_lock = _NullLock()
        handler._semantic_search_tool = MagicMock()
        handler._semantic_search_tool.function = AsyncMock(return_value="ok")
        handler.ingestor = MagicMock()
        handler.ingestor.list_projects = MagicMock(return_value=[ALPHA, BETA])

        await handler.semantic_search("intent")

        kwargs = handler._semantic_search_tool.function.call_args.kwargs
        assert kwargs["project"] is None


def _prefixes(result: dict) -> set[str]:
    return {row["qualified_name"].split(".")[0] for row in result["results"]}


def _handler_returning(rows: list[dict]):
    """An MCPTools bound to a stub graph, with the real handler logic."""
    from unittest.mock import AsyncMock, MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry
    from codebase_rag.schemas import QueryGraphData

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler._ingestor_lock = _NullLock()
    handler._query_tool = MagicMock()
    handler._query_tool.function = AsyncMock(
        return_value=QueryGraphData(
            query_used="MATCH (n) RETURN n",
            results=list(rows),
            summary="ok",
        )
    )
    handler.ingestor = MagicMock()
    handler.ingestor.list_projects = MagicMock(return_value=[ALPHA, BETA])
    return handler


class _NullLock:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_: object) -> None:
        return None


class TestEnforcementSurvivesAnUnfilteredQuery:
    """The case a prompt cannot prevent.

    The model generates the Cypher. If it omits the project filter -- which
    is exactly what produced the reported bleed -- the guarantee has to come
    from somewhere else.
    """

    @pytest.mark.asyncio
    async def test_an_unfiltered_generated_query_still_returns_one_project(
        self,
    ) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.tools.codebase_query import create_query_tool

        # Deliberately NO project filter in the generated Cypher.
        cypher_gen = MagicMock()

        async def _generate(_query: str) -> str:
            return "MATCH (n:Function) RETURN n.qualified_name, n.name"

        cypher_gen.generate = _generate

        ingestor = MagicMock()
        ingestor.fetch_all = MagicMock(return_value=list(_ROWS))

        tool = create_query_tool(ingestor, cypher_gen, project_name=ALPHA)
        result = await tool.function("every function")

        names = [row["qualified_name"] for row in result.results]

        assert names, "scoping emptied the result entirely"
        assert all(n.startswith(f"{ALPHA}.") for n in names), names

    @pytest.mark.asyncio
    async def test_without_a_project_the_same_query_returns_both(self) -> None:
        """The control.

        Without it, a `create_query_tool` that returned nothing at all would
        satisfy the test above -- "no rows from other projects" is trivially
        true of an empty result.
        """
        from unittest.mock import MagicMock

        from codebase_rag.tools.codebase_query import create_query_tool

        cypher_gen = MagicMock()

        async def _generate(_query: str) -> str:
            return "MATCH (n:Function) RETURN n.qualified_name, n.name"

        cypher_gen.generate = _generate

        ingestor = MagicMock()
        ingestor.fetch_all = MagicMock(return_value=list(_ROWS))

        tool = create_query_tool(ingestor, cypher_gen)
        result = await tool.function("every function")

        prefixes = {row["qualified_name"].split(".")[0] for row in result.results}

        assert prefixes == {ALPHA, BETA}
