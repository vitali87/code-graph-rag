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
        """One project name must not swallow a sibling that extends it.

        A bare `startswith(project)` would match a DIFFERENT project whose
        name begins with this one, so the separator is required.

        The fixture uses names `derive_project_name` can actually produce.
        An earlier version used `alpha__aaaa1111_extra`, which that
        function CANNOT emit -- it always ends `__<8 hex digits>` -- so the
        test was asserting behaviour on a string no project could have.
        A directory named `alpha_extra` really does yield the name below.
        """
        from pathlib import Path

        from codebase_rag.tools.codebase_query import scope_rows_to_project
        from codebase_rag.utils.path_utils import derive_project_name

        base = derive_project_name(Path("/tmp/alpha"))
        sibling = derive_project_name(Path("/tmp/alpha_extra"))
        assert sibling.startswith("alpha_"), sibling

        rows = [
            {"qualified_name": f"{base}.real.Thing"},
            {"qualified_name": f"{sibling}.other.Thing"},
        ]

        kept = scope_rows_to_project(rows, base)

        assert [r["qualified_name"] for r in kept] == [f"{base}.real.Thing"]

    def test_a_row_keyed_on_something_other_than_qualified_name_is_scoped(
        self,
    ) -> None:
        """`RETURN a.qualified_name AS from_qn` must not evade the filter.

        The repo's own relationship queries return `from_qn`/`to_qn`, and a
        model may name a column anything. Keying on the literal
        `qualified_name` key made the filter FAIL OPEN for exactly those
        shapes: the row looked like an aggregate and was kept.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [
            {"from_qn": f"{ALPHA}.a.f", "to_qn": f"{ALPHA}.b.g"},
            {"from_qn": f"{BETA}.a.f", "to_qn": f"{BETA}.b.g"},
        ]

        kept = scope_rows_to_project(rows, ALPHA)

        assert [r["from_qn"] for r in kept] == [f"{ALPHA}.a.f"]

    def test_a_row_naming_another_project_anywhere_is_dropped(self) -> None:
        """Any value identifying a foreign project disqualifies the row.

        An edge spanning two projects is not a row a scoped caller asked
        for, and keeping it would leak the other project's names.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"from_qn": f"{ALPHA}.a.f", "to_qn": f"{BETA}.b.g"}]

        assert scope_rows_to_project(rows, ALPHA) == []

    def test_a_genuine_aggregate_still_survives(self) -> None:
        """The control that keeps the default-deny rule honest.

        Tightening this filter must not start discarding `RETURN count(n)`.
        A row carrying no project-identifying value at all is kept.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"total": 42, "label": "Function"}]

        assert scope_rows_to_project(rows, ALPHA) == rows

    def test_free_text_containing_a_dot_is_not_mistaken_for_a_qn(self) -> None:
        """A docstring or path must not be read as a qualified name.

        Over-eager matching would drop legitimate rows whose text happens
        to contain dots -- the too-aggressive failure direction.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [
            {
                "qualified_name": f"{ALPHA}.mod.f",
                "docstring": "Reads config.yaml and writes out.json",
                "path": "src/alpha/mod.py",
            }
        ]

        assert scope_rows_to_project(rows, ALPHA) == rows

    def test_a_row_carrying_no_project_evidence_cannot_be_scoped(self) -> None:
        """The limit of a result-level filter, asserted rather than assumed.

        `RETURN n.name, n.path` produces rows with nothing identifying the
        project, so this filter CANNOT tell them apart -- and it keeps them,
        because it cannot prove they are foreign either.

        Documenting it as a test because the gap is invisible otherwise: the
        filter looks like it scopes everything. The guarantee is completed
        by `requires_project_evidence` below, which refuses such a query up
        front rather than answering it with unscoped rows.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [
            {"name": "handler", "path": "alpha/service.py"},
            {"name": "handler", "path": "beta/service.py"},
        ]

        assert scope_rows_to_project(rows, ALPHA) == rows


class TestTheCliPathAppliesTheEvidenceGuardToo:
    """The guard must not live only in the MCP handler.

    A scoped CLI session whose query returns `n.name` and `n.path` gets
    rows that cannot be attributed to a project. Filtering keeps them, so
    without the guard here the CLI silently ignores its own scope -- the
    same gap I closed for MCP, left open one layer down.
    """

    @pytest.mark.asyncio
    async def test_a_scoped_tool_refuses_an_unattributable_query(self) -> None:
        from unittest.mock import MagicMock

        from codebase_rag.tools.codebase_query import create_query_tool

        cypher_gen = MagicMock()

        async def _generate(_query: str) -> str:
            return "MATCH (n:Function) RETURN n.name AS name, n.path AS path"

        cypher_gen.generate = _generate
        ingestor = MagicMock()
        ingestor.fetch_all = MagicMock(
            return_value=[
                {"name": "handler", "path": "a.py"},
                {"name": "handler", "path": "b.py"},
            ]
        )

        tool = create_query_tool(ingestor, cypher_gen, project_name=ALPHA)
        result = await tool.function("names only")

        assert result.results == []
        assert "scope" in result.summary.lower() or "project" in result.summary.lower()

    @pytest.mark.asyncio
    async def test_the_same_query_is_fine_unscoped(self) -> None:
        """The control: the refusal follows from scoping, not from the query."""
        from unittest.mock import MagicMock

        from codebase_rag.tools.codebase_query import create_query_tool

        cypher_gen = MagicMock()

        async def _generate(_query: str) -> str:
            return "MATCH (n:Function) RETURN n.name AS name, n.path AS path"

        cypher_gen.generate = _generate
        ingestor = MagicMock()
        ingestor.fetch_all = MagicMock(
            return_value=[
                {"name": "handler", "path": "a.py"},
                {"name": "handler", "path": "b.py"},
            ]
        )

        tool = create_query_tool(ingestor, cypher_gen)
        result = await tool.function("names only")

        assert len(result.results) == 2


class TestTheMarkerDoesNotCatchOrdinaryNames:
    """`__init__` is not a project name.

    The digest marker `__` is what separates a qualified name from free
    text -- but Python's most common method name contains it, and so does
    every dunder. Treating `__init__` as a foreign qualified name DROPS a
    valid row, which is the too-aggressive direction.
    """

    @pytest.mark.parametrize(
        "value", ["__init__", "__main__", "__repr__", "_private", "__"]
    )
    def test_a_dunder_name_does_not_disqualify_a_row(self, value: str) -> None:
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.mod.C", "name": value}]

        assert scope_rows_to_project(rows, ALPHA) == rows

    def test_a_real_foreign_qualified_name_is_still_dropped(self) -> None:
        """The control: narrowing the marker must not reopen the leak."""
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok"}, {"qualified_name": f"{BETA}.leak"}]

        kept = scope_rows_to_project(rows, ALPHA)

        assert [r["qualified_name"] for r in kept] == [f"{ALPHA}.ok"]


class TestNestedValuesAreScoped:
    """A foreign name inside a list or dict must not ride through.

    Only top-level string values were inspected, so `collect(...)` results
    and map projections carried other projects' qualified names untouched.
    """

    def test_a_foreign_name_in_a_list_disqualifies_the_row(self) -> None:
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok", "callees": [f"{BETA}.secret.fn"]}]

        assert scope_rows_to_project(rows, ALPHA) == []

    def test_a_foreign_name_in_a_dict_disqualifies_the_row(self) -> None:
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok", "meta": {"ref": f"{BETA}.other"}}]

        assert scope_rows_to_project(rows, ALPHA) == []

    def test_an_own_project_name_nested_is_kept(self) -> None:
        """The control: nesting must not become a blanket rejection."""
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok", "callees": [f"{ALPHA}.other.fn"]}]

        assert scope_rows_to_project(rows, ALPHA) == rows


class TestEvidenceMustBeInTheReturnClause:
    """`qualified_name` in a WHERE clause is not evidence.

    The check was a substring match over the whole query, so filtering on a
    qualified name while RETURNING only `n.name` satisfied it -- and those
    rows are exactly the unattributable ones the guard exists to refuse.
    """

    def test_a_where_only_mention_is_not_evidence(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            'MATCH (n) WHERE n.qualified_name STARTS WITH "x" RETURN n.name AS name'
        )

    def test_an_order_by_only_mention_is_not_evidence(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n) RETURN n.name AS name ORDER BY n.qualified_name"
        )

    def test_a_returned_qualified_name_is_evidence(self) -> None:
        """The control, including the WHERE-plus-RETURN case that is fine."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            'MATCH (n) WHERE n.name = "x" RETURN n.qualified_name AS qualified_name'
        )

    def test_an_aggregate_mixed_with_unqualified_fields_is_not_evidence(self) -> None:
        """`RETURN n.name, count(n)` still exposes names.

        The aggregate exemption was meant for `RETURN count(n)` alone, which
        names nobody. Mixing it with a bare field leaks exactly what the
        exemption assumed could not leak.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n:Function) RETURN n.name AS name, count(n) AS total"
        )

    def test_an_aggregate_must_itself_be_restricted(self) -> None:
        """A pure aggregate is evidence ONLY if the query narrows the rows.

        This assertion previously read the other way -- that
        `RETURN count(n)` alone was evidence, on the reasoning that an
        aggregate names nobody. It encoded a real leak: the count spans
        every indexed project, so a scoped caller learns the SIZE of
        projects they never asked about. Names were not the only thing
        that leaks.

        Counting stays usable when scoped, via the restricted form below.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n:Function) RETURN count(n) AS total"
        )
        assert requires_project_evidence(
            "MATCH (n:Function) WHERE n.qualified_name STARTS WITH $p "
            "RETURN count(n) AS total"
        )


class TestEvidenceIsAProjectedPropertyNotASubstring:
    """An ALIAS containing "qualified_name" is not evidence.

    The check matched the token anywhere in the RETURN clause, so
    `RETURN n.name AS qualified_name_of_thing` satisfied it while
    returning no qualified name at all -- unattributable rows, kept.
    """

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (n) RETURN n.name AS qualified_name_of_thing",
            'MATCH (n) RETURN "qualified_name" AS lit',
            "MATCH (n) RETURN n.path AS my_qualified_name_col",
            # A DIFFERENT property whose name merely starts with the token.
            # This is the case the trailing boundary exists for: the three
            # above are already rejected by the required `<ident>.` prefix,
            # so without this one a mutation dropping the boundary survives.
            "MATCH (n) RETURN n.qualified_name_extra AS x",
        ],
    )
    def test_a_mere_mention_is_not_evidence(self, cypher: str) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(cypher)

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (n) RETURN n.qualified_name",
            "MATCH (n) RETURN n.qualified_name AS anything",
            "MATCH (a)-[r]->(b) RETURN a.qualified_name AS from_qn, b.qualified_name",
        ],
    )
    def test_a_projected_property_is_evidence(self, cypher: str) -> None:
        """The control: reading the PROPERTY must still count."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(cypher)


class TestAnAggregateLeaksMagnitude:
    """`RETURN count(n)` exposes no NAMES but still exposes a NUMBER.

    The exemption reasoned about name leakage and missed that a scoped
    caller receiving a count over every indexed project learns the size
    of projects they did not ask about. An unfiltered aggregate is not
    safe merely because it is anonymous.
    """

    def test_an_unfiltered_aggregate_is_refused_when_scoped(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence("MATCH (n:Function) RETURN count(n)")

    def test_an_aggregate_over_a_scoped_match_is_accepted(self) -> None:
        """A count the query itself restricts is attributable and fine.

        This is what keeps counting usable when scoped: the caller asks
        for a project-filtered count and gets one.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            "MATCH (n:Function) WHERE n.qualified_name STARTS WITH $p RETURN count(n)"
        )


class TestEveryProjectedEntityMustCarryItsQualifiedName:
    """One entity's qualified name does not vouch for another's properties.

    `MATCH (a)-[x]->(b) RETURN a.qualified_name, b.name, b.path` returns a
    row that is PARTLY in-project: `a` proves membership, while `b.name`
    and `b.path` belong to an entity whose qualified name was never
    projected. The row filter keeps the row -- one value proves membership
    -- and beta's function name and file path reach an alpha-scoped
    caller.

    The row filter cannot fix this. Dropping the row loses legitimate data
    and keeping it leaks, so the query is refused instead, exactly as for
    a row with no evidence at all.
    """

    def test_a_second_entity_without_its_qualified_name_is_refused(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (a)-[x]->(b) RETURN a.qualified_name AS qualified_name, "
            "b.name AS name, b.path AS path"
        )

        assert not requires_project_evidence(cypher, ALPHA)

    def test_both_entities_projecting_a_qualified_name_is_accepted(self) -> None:
        """The control: the legitimate relationship query must survive.

        Every entity whose properties are returned carries its own
        qualified name, so every row is attributable and the row filter
        can judge it.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (a)-[x]->(b) RETURN a.qualified_name AS from_qn, "
            "b.qualified_name AS to_qn, b.name AS name"
        )

        assert requires_project_evidence(cypher, ALPHA)

    def test_one_entity_projecting_several_properties_is_accepted(self) -> None:
        """The other control: this must not forbid ordinary single-entity queries.

        `n.qualified_name, n.name, n.path` is one entity with three
        properties, not two entities. Requiring a qualified name per
        ALIAS rather than per ENTITY would refuse it.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (n:Function) RETURN n.qualified_name AS qualified_name, "
            "n.name AS name, n.path AS path"
        )

        assert requires_project_evidence(cypher, ALPHA)


class TestAnAggregateMustBeBoundToTheRequestedProject:
    """Restricting by *a* qualified name is not restricting to *yours*.

    The aggregate exemption checked only that the query filtered on some
    qualified name, so a query narrowing to a DIFFERENT project passed and
    returned that project's count to the scoped caller. The same magnitude
    leak the exemption was tightened to prevent, one level deeper.
    """

    def test_a_restriction_naming_another_project_is_refused(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{BETA}." '
            "RETURN count(n) AS total"
        )

        assert not requires_project_evidence(cypher, ALPHA)

    def test_a_restriction_naming_the_requested_project_is_accepted(self) -> None:
        """The control: the useful case must survive the tightening."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total"
        )

        assert requires_project_evidence(cypher, ALPHA)

    def test_a_parameterised_restriction_is_accepted(self) -> None:
        """`$project` carries no literal to compare, and is how the tool asks.

        Refusing it would make the parameterised form -- the safe one --
        unusable, pushing callers towards string interpolation.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (n) WHERE n.qualified_name STARTS WITH $project "
            "RETURN count(n) AS total"
        )

        assert requires_project_evidence(cypher, ALPHA)

    def test_a_projected_qualified_name_needs_no_binding(self) -> None:
        """The control that keeps the non-aggregate path unchanged.

        A query RETURNING qualified names is attributable row by row, so
        `scope_rows_to_project` filters it and the binding check does not
        apply. Requiring binding there would refuse valid queries.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            "MATCH (n) RETURN n.qualified_name AS qualified_name", ALPHA
        )


class TestScopedQueriesMustProjectAQualifiedName:
    """Close the gap the result filter cannot.

    A scoped request whose query returns no qualified name is refused, so
    the caller learns the scope could not be honoured instead of silently
    receiving every project's rows.
    """

    def test_a_query_without_a_qualified_name_is_refused(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n:Function) RETURN n.name AS name, n.path AS path"
        )

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (n:Function) RETURN n.qualified_name AS qualified_name",
            "MATCH (a)-[r]->(b) RETURN a.qualified_name AS from_qn",
            "MATCH (n) RETURN n.qualified_name",
        ],
    )
    def test_a_query_projecting_a_qualified_name_is_accepted(self, cypher: str) -> None:
        """Any projection of a qualified name suffices, whatever it is aliased to.

        The alias is the model's choice, so requiring a specific column name
        would refuse valid queries.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(cypher)

    def test_a_restricted_aggregate_is_accepted(self) -> None:
        """Counting stays usable when scoped, provided the query narrows.

        This previously asserted that a BARE `RETURN count(n)` was
        accepted, reasoning that it leaks no names. It leaks a magnitude
        instead -- see TestAnAggregateLeaksMagnitude. The restricted form
        preserves the useful case without the leak.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            "MATCH (n:Function) WHERE n.qualified_name STARTS WITH $p "
            "RETURN count(n) AS total"
        )


class TestUnscopedIsUnchanged:
    """Every existing caller passes no project."""

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


class TestTheHandlerRefusesAnUnjudgeableScopedQuery:
    """The gap the result filter cannot close, closed at the handler.

    `RETURN n.name, n.path` yields rows carrying no project evidence, so
    the filter keeps them -- it cannot prove them foreign. Answering a
    SCOPED request with those rows would silently ignore the scope, which
    is the original bug wearing a different hat.
    """

    @pytest.mark.asyncio
    async def test_a_scoped_query_returning_no_qualified_name_is_refused(
        self,
    ) -> None:
        handler = _handler_returning(
            [{"name": "handler", "path": "a.py"}],
            query_used="MATCH (n:Function) RETURN n.name AS name, n.path AS path",
        )

        result = await handler.query_code_graph("names only", project=ALPHA)

        assert result.get("error"), result
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_the_same_query_is_fine_unscoped(self) -> None:
        """The control: the refusal is a consequence of scoping, not a ban.

        Without this, refusing that query outright would pass the test
        above while breaking every existing unscoped caller.
        """
        handler = _handler_returning(
            [{"name": "handler", "path": "a.py"}],
            query_used="MATCH (n:Function) RETURN n.name AS name, n.path AS path",
        )

        result = await handler.query_code_graph("names only")

        assert not result.get("error")
        assert len(result["results"]) == 1


class TestTheCliScopeIsChosenNotAssumed:
    """The CLI shares the weakness, but only when one project is active.

    A session with several projects activated has asked for all of them, so
    scoping to one would silently narrow deliberate multi-project work. The
    rule is therefore "exactly one active project", not "the first one".
    """

    def test_one_active_project_is_the_scope(self) -> None:
        from codebase_rag.main import _cli_query_scope

        assert _cli_query_scope([ALPHA]) == ALPHA

    def test_several_active_projects_means_no_scope(self) -> None:
        """The control that stops this narrowing deliberate multi-project use."""
        from codebase_rag.main import _cli_query_scope

        assert _cli_query_scope([ALPHA, BETA]) is None

    def test_no_active_projects_means_no_scope(self) -> None:
        """The default path: nothing activated, so nothing is excluded."""
        from codebase_rag.main import _cli_query_scope

        assert _cli_query_scope(None) is None
        assert _cli_query_scope([]) is None


class TestTheHandlerBindsTheAggregateToItsOwnProject:
    """The MCP call site must pass the project, not just possess one.

    `requires_project_evidence` takes the project as a DEFAULTED parameter,
    so a call site that forgets it still type-checks, still runs, and still
    returns a well-formed answer -- the guard just stops binding. Every
    other test here observes that function's return value, which is
    identical whether or not the handler passed anything.

    Verified by mutation: dropping `project` from the handler's call passed
    all 56 other tests. This is the one that fails.
    """

    @pytest.mark.asyncio
    async def test_a_cross_project_aggregate_is_refused_through_the_handler(
        self,
    ) -> None:
        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{BETA}." '
            "RETURN count(n) AS total"
        )
        handler = _handler_returning([{"total": 99}], query_used=cypher)

        result = await handler.query_code_graph("how many", project=ALPHA)

        assert result.get("error"), result
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_an_own_project_aggregate_is_allowed_through_the_handler(
        self,
    ) -> None:
        """The control: binding must not refuse the caller's own count."""
        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total"
        )
        handler = _handler_returning([{"total": 99}], query_used=cypher)

        result = await handler.query_code_graph("how many", project=ALPHA)

        assert not result.get("error"), result
        assert result["results"] == [{"total": 99}]


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


def _handler_returning(
    rows: list[dict],
    query_used: str = "MATCH (n) RETURN n.qualified_name AS qualified_name",
):
    """An MCPToolsRegistry bound to a stub graph, with the real handler logic.

    The default `query_used` PROJECTS a qualified name, because that is what
    a scoped request needs in order to be judgeable. Tests that want the
    unjudgeable case pass their own.
    """
    from unittest.mock import AsyncMock, MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry
    from codebase_rag.schemas import QueryGraphData

    handler = MCPToolsRegistry.__new__(MCPToolsRegistry)
    handler._ingestor_lock = _NullLock()
    handler._query_tool = MagicMock()
    handler._query_tool.function = AsyncMock(
        return_value=QueryGraphData(
            query_used=query_used,
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
