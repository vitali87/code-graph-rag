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

from pathlib import Path

import pytest

from codebase_rag import constants as cs

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

    def test_a_foreign_name_in_a_dict_KEY_disqualifies_the_row(self) -> None:
        """A map keyed BY qualified name leaks through its keys.

        `_names_another_project` recursed into `dict.values()` and never
        `dict.keys()`, so a map projection keyed by qualified name carried
        foreign names in a position nothing inspected.

        Found by auditing my own docstring: it claimed "every string VALUE
        in the row is inspected, at any depth", and I wrote that describing
        the recursion rather than enumerating what Cypher can return. Keys
        are values too.
        """
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok", "by_qn": {f"{BETA}.secret": 3}}]

        assert scope_rows_to_project(rows, ALPHA) == []

    def test_an_own_project_key_is_kept(self) -> None:
        """The control: keys naming the caller's own project must survive."""
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"qualified_name": f"{ALPHA}.ok", "by_qn": {f"{ALPHA}.mine": 3}}]

        assert scope_rows_to_project(rows, ALPHA) == rows

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
            'MATCH (n) WHERE n.qualified_name STARTS WITH "x" RETURN n.name AS name',
            ALPHA,
        )

    def test_an_order_by_only_mention_is_not_evidence(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n) RETURN n.name AS name ORDER BY n.qualified_name",
            ALPHA,
        )

    def test_a_returned_qualified_name_is_evidence(self) -> None:
        """The control, including the WHERE-plus-RETURN case that is fine."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            'MATCH (n) WHERE n.name = "x" RETURN n.qualified_name AS qualified_name',
            ALPHA,
        )

    def test_an_aggregate_mixed_with_unqualified_fields_is_not_evidence(self) -> None:
        """`RETURN n.name, count(n)` still exposes names.

        The aggregate exemption was meant for `RETURN count(n)` alone, which
        names nobody. Mixing it with a bare field leaks exactly what the
        exemption assumed could not leak.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n:Function) RETURN n.name AS name, count(n) AS total",
            ALPHA,
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
            "MATCH (n:Function) RETURN count(n) AS total",
            ALPHA,
        )
        assert requires_project_evidence(
            f'MATCH (n:Function) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total",
            ALPHA,
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

        assert not requires_project_evidence(cypher, ALPHA)

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

        assert requires_project_evidence(cypher, ALPHA)


class TestAnAggregateLeaksMagnitude:
    """`RETURN count(n)` exposes no NAMES but still exposes a NUMBER.

    The exemption reasoned about name leakage and missed that a scoped
    caller receiving a count over every indexed project learns the size
    of projects they did not ask about. An unfiltered aggregate is not
    safe merely because it is anonymous.
    """

    def test_an_unfiltered_aggregate_is_refused_when_scoped(self) -> None:
        """`ALPHA` is passed so the refusal comes from the aggregate rule.

        Without a project name, `_restricts_to_project` returns False at its
        `if not project_name` guard, so the assertion held whatever the
        aggregate logic did -- a test named for the SCOPED path that never
        reached it (reported on #1494).
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(
            "MATCH (n:Function) RETURN count(n)", ALPHA
        )

    def test_an_aggregate_over_a_scoped_match_is_accepted(self) -> None:
        """A count the query itself restricts is attributable and fine.

        This is what keeps counting usable when scoped: the caller asks
        for a project-filtered count and gets one.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            f'MATCH (n:Function) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n)",
            ALPHA,
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


class TestOnlyARestrictivePredicateAuthorisesAnAggregate:
    """Mentioning a qualified name is not constraining by one.

    `WHERE n.qualified_name IS NOT NULL` matches every indexed node in
    every project, so a count over it spans them all -- yet it satisfied a
    check that only looked for the property appearing before RETURN. The
    predicate has to actually narrow to a prefix.
    """

    @pytest.mark.parametrize(
        "predicate",
        [
            "n.qualified_name IS NOT NULL",
            "n.qualified_name <> ''",
            "exists(n.qualified_name)",
        ],
    )
    def test_a_nonrestrictive_predicate_is_refused(self, predicate: str) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = f"MATCH (n) WHERE {predicate} RETURN count(n) AS total"

        assert not requires_project_evidence(cypher, ALPHA)

    def test_a_prefix_predicate_is_accepted(self) -> None:
        """The control: the restrictive form must still authorise a count."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total"
        )

        assert requires_project_evidence(cypher, ALPHA)


class TestQuotedTextIsNotEvidence:
    """A qualified name inside a string literal returns no qualified name.

    `RETURN "n.qualified_name" AS lit, n.name AS name` projects a constant
    and an unattributable property, but textual matching saw the property
    read inside the quotes.
    """

    def test_a_quoted_property_read_is_not_evidence(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = 'MATCH (n) RETURN "n.qualified_name" AS lit, n.name AS name'

        assert not requires_project_evidence(cypher, ALPHA)

    def test_a_single_quoted_property_read_is_not_evidence(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = "MATCH (n) RETURN 'n.qualified_name' AS lit, n.name AS name"

        assert not requires_project_evidence(cypher, ALPHA)

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (n) RETURN n.name AS name // n.qualified_name",
            "MATCH (n) RETURN n.name AS name /* n.qualified_name */",
            "MATCH (n) // n.qualified_name\n RETURN n.name AS name",
        ],
    )
    def test_a_commented_property_read_is_not_evidence(self, cypher: str) -> None:
        """A qualified name in a COMMENT returns nothing either.

        I tested only the before-RETURN form first and called the class
        handled. The after-RETURN forms leaked, because the projection is
        taken from the text following the last RETURN and a trailing
        comment sits inside it. Same unenumerated-axis mistake as the
        dunder case: the right property, the wrong positions.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(cypher, ALPHA)

    def test_a_real_read_beside_a_comment_is_still_evidence(self) -> None:
        """The control: stripping comments must not blind the real check."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (n) RETURN n.qualified_name AS qualified_name "
            "// the project-qualified name"
        )

        assert requires_project_evidence(cypher, ALPHA)

    def test_a_real_read_beside_a_quoted_one_is_still_evidence(self) -> None:
        """The control: stripping literals must not blind the real check.

        A query can legitimately return a string constant alongside real
        properties, and that must still be judged on the real ones.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (n) RETURN 'a label' AS kind, "
            "n.qualified_name AS qualified_name, n.name AS name"
        )

        assert requires_project_evidence(cypher, ALPHA)


class TestOnlyTheSimpleShapeIsScopeable:
    """Default-deny on STRUCTURE, not a list of known bypasses.

    Four review rounds found four ways to satisfy a textual evidence check
    while returning unattributable data: an alias containing the token, a
    string literal, a comment, and a transformed projection. Two more used
    UNION so that only the final branch was inspected. Enumerating them is
    endless -- every Cypher construct not yet considered is a candidate.

    So a SCOPED query must have the shape the prompt already mandates:
    "MATCH, WHERE, RETURN, LIMIT" with plain aliased property reads. Any
    other structure is refused rather than analysed. The model is told to
    emit exactly this, so refusing the rest costs nothing it should be
    producing, and it converts an open-ended class into a closed one.
    """

    @pytest.mark.parametrize(
        "cypher",
        [
            # UNION: only the last branch was inspected, so an earlier one
            # could return anything.
            "MATCH (n) RETURN n.name AS name "
            "UNION MATCH (m) RETURN m.qualified_name AS name",
            # A transformed qualified name does not attribute its entity.
            "MATCH (a),(b) RETURN a.qualified_name AS q, "
            "left(b.qualified_name, 3) AS t, b.name AS n",
            # A CALL SUBQUERY assembles its projection elsewhere.
            "CALL { MATCH (n) RETURN n.name AS name } RETURN name",
            "MATCH (n) WITH n.name AS name RETURN name",
        ],
    )
    def test_an_unrecognised_structure_is_refused_when_scoped(
        self, cypher: str
    ) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert not requires_project_evidence(cypher, ALPHA)

    @pytest.mark.parametrize(
        "cypher",
        [
            "MATCH (n:Function) RETURN n.qualified_name AS qualified_name",
            "MATCH (n:Function) RETURN n.qualified_name AS qualified_name, "
            "n.name AS name, n.path AS path LIMIT 50",
            "MATCH (a)-[r]->(b) RETURN a.qualified_name AS from_qn, "
            "b.qualified_name AS to_qn",
            # prompts.py line 64 RECOMMENDS `ENDS WITH` for matching a
            # class or function by its short name, so refusing it would
            # break the form the prompt teaches.
            'MATCH (c) WHERE c.qualified_name ENDS WITH ".VatManager" '
            "RETURN c.qualified_name AS qualified_name",
        ],
    )
    def test_the_shape_the_prompt_asks_for_is_accepted(self, cypher: str) -> None:
        """The control, and the reason this is not simply a ban.

        These are the forms the system prompt instructs the model to emit
        -- MATCH/WHERE/RETURN/LIMIT with aliased property reads. If the
        default-deny rule refused them it would break every scoped query,
        which a leak-only test suite would not notice.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(cypher, ALPHA)

    @pytest.mark.asyncio
    async def test_an_unrecognised_structure_is_fine_unscoped(self) -> None:
        """The other control: this restricts SCOPED requests only.

        An unscoped caller asked for everything, so a UNION or a subquery
        is none of this guard's business. Asserted through the HANDLER,
        because that is where the scope decision lives -- asserting on
        `requires_project_evidence` directly would say nothing, since the
        handler never calls it when no project is given.
        """
        cypher = "MATCH (n) RETURN n.name AS name UNION MATCH (m) RETURN m.name AS name"
        handler = _handler_returning([{"name": "x"}], query_used=cypher)

        result = await handler.query_code_graph("everything")

        assert not result.get("error"), result
        assert result["results"] == [{"name": "x"}]


class TestAnAggregatedEntityMustAlsoBeAttributable:
    """`count(b)` exposes b's magnitude even when `a` vouches for the row.

    `MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name, count(b)` was
    accepted: `a` is attributable, so the row passed, while the count
    reported how many `b` there are across every project.

    The per-entity check SKIPPED aggregate terms entirely, on the reasoning
    that an aggregate names nobody. It names nobody but it still measures
    someone, which is the magnitude leak in a mixed projection.
    """

    def test_an_aggregate_over_an_unattributed_entity_is_refused(self) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name AS q, count(b) AS total"
        )

        assert not requires_project_evidence(cypher, ALPHA)

    def test_an_aggregate_over_the_same_attributed_entity_is_accepted(self) -> None:
        """The control: counting the entity you already attributed is fine.

        `RETURN a.qualified_name, count(a)` measures only rows the filter
        can judge, so refusing it would break a legitimate shape.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = "MATCH (a) RETURN a.qualified_name AS q, count(a) AS total"

        assert requires_project_evidence(cypher, ALPHA)

    def test_an_aggregate_over_an_attributed_second_entity_is_accepted(self) -> None:
        """The other control: attribute b too and the count is fine.

        This is what the caller should write, and it must stay available --
        a rule that refused every mixed aggregate would pass the leak test
        while making relationship counting impossible when scoped.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            "MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name AS q, "
            "b.qualified_name AS bq, count(b) AS total"
        )

        assert requires_project_evidence(cypher, ALPHA)


class TestTheRestrictionMustConstrainTheAggregatedAlias:
    """Restricting alias `a` says nothing about a count over alias `b`.

    `MATCH (a),(b) WHERE a.qualified_name STARTS WITH "alpha." RETURN
    count(b)` was accepted: a literal naming the requested project is
    present, so the restriction check passed -- while the count ranged
    over `b`, which nothing constrained.

    The check asked "is there a restriction to my project", not "is the
    thing being COUNTED restricted". Those differ whenever the query
    matches more than one entity.
    """

    def test_restricting_one_alias_does_not_authorise_counting_another(
        self,
    ) -> None:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
        )

        assert not requires_project_evidence(cypher, ALPHA)

    def test_restricting_the_counted_alias_is_accepted(self) -> None:
        """The control: constrain what you count and the count is sound."""
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (b) WHERE b.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
        )

        assert requires_project_evidence(cypher, ALPHA)

    def test_restricting_both_aliases_is_accepted(self) -> None:
        """The other control: a relationship count with both ends bound.

        This is the legitimate form of the leaking query, and it must stay
        available -- refusing every multi-alias aggregate would pass the
        leak test while breaking scoped relationship counting.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (a)-[:CALLS]->(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            f'AND b.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
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

    @pytest.mark.parametrize(
        "predicate",
        [
            # A parameter this code cannot see the value of. Accepting it
            # means accepting a restriction to ANY project.
            "n.qualified_name STARTS WITH $anything",
            "n.qualified_name =~ $re",
            # Restricts n to m, which says nothing about the caller.
            "n.qualified_name = m.qualified_name",
        ],
    )
    def test_a_literal_free_restriction_is_refused(self, predicate: str) -> None:
        """ "No literal" was read as "safely parameterised". It is not.

        The exemption accepted any literal-free query carrying a prefix
        operator, on the reasoning that a parameter must be the project.
        But this code cannot see a parameter's VALUE, so it was accepting
        a restriction to any project at all -- or, in the last case, to
        another matched entity.

        The only form it can actually verify is the literal one, and the
        caller knows their own project name, so that is what is required.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = f"MATCH (n),(m) WHERE {predicate} RETURN count(n) AS total"

        assert not requires_project_evidence(cypher, ALPHA)

    def test_a_literal_restriction_to_the_caller_is_accepted(self) -> None:
        """The control: the verifiable form must stay usable.

        This is the one shape that proves the count belongs to the caller,
        and the prompt already instructs the model to emit it.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        cypher = (
            f'MATCH (n) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
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
            "MATCH (n:Function) RETURN n.name AS name, n.path AS path",
            ALPHA,
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

        assert requires_project_evidence(cypher, ALPHA)

    def test_a_restricted_aggregate_is_accepted(self) -> None:
        """Counting stays usable when scoped, provided the query narrows.

        This previously asserted that a BARE `RETURN count(n)` was
        accepted, reasoning that it leaks no names. It leaks a magnitude
        instead -- see TestAnAggregateLeaksMagnitude. The restricted form
        preserves the useful case without the leak.
        """
        from codebase_rag.tools.codebase_query import requires_project_evidence

        assert requires_project_evidence(
            f'MATCH (n:Function) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total",
            ALPHA,
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


class TestTheDeclaredSchemaMatchesTheHandler:
    """A parameter absent from the schema is unreachable over the protocol.

    The handlers accept `project`, but an MCP client reads the declared
    input schema to learn what it may send. Omitting it there makes the
    whole feature undiscoverable and, for a strict client, unsendable --
    the code works and nothing can call it.

    This is the same silent-disconnection shape as an unwired parameter:
    every other test drives the handler DIRECTLY, bypassing the schema, so
    they all pass regardless.
    """

    @pytest.mark.parametrize(
        "tool_name",
        [cs.MCPToolName.QUERY_CODE_GRAPH, cs.MCPToolName.SEMANTIC_SEARCH],
    )
    def test_project_is_declared_for_a_scopeable_tool(
        self, tool_name: str, tmp_path: Path
    ) -> None:
        schema = _declared_schema(tmp_path, tool_name)

        assert cs.MCPParamName.PROJECT in schema["properties"], (
            f"{tool_name} accepts a project but does not declare it, so no "
            "client can send one"
        )

    @pytest.mark.parametrize(
        "tool_name",
        [cs.MCPToolName.QUERY_CODE_GRAPH, cs.MCPToolName.SEMANTIC_SEARCH],
    )
    def test_project_is_optional(self, tool_name: str, tmp_path: Path) -> None:
        """The control: declaring it must not make it mandatory.

        Every existing client sends no project and must keep working.
        """
        schema = _declared_schema(tmp_path, tool_name)

        assert cs.MCPParamName.PROJECT not in (schema.get("required") or [])


def _declared_schema(tmp_path: Path, tool_name: str):
    """The input schema the registry publishes for `tool_name`.

    Built through the real constructor, since `_tools` is assembled in
    `__init__` -- reading it off a hand-made object would test a dict this
    code never publishes.

    `semantic_search` is registered only when the vector backend is
    installed, so on a BASE install the tool legitimately does not exist and
    a `KeyError` here is the environment, not a regression. Skipping on the
    bare absence would fail OPEN -- a tool wrongly dropped on a full install
    would skip rather than fail -- so the skip is conditioned on
    `has_semantic_dependencies()`, the same predicate the registry itself
    gates on. Absent while the dependencies ARE present still fails.
    """
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry
    from codebase_rag.utils.dependencies import has_semantic_dependencies

    registry = MCPToolsRegistry(
        project_root=str(tmp_path),
        ingestor=MagicMock(),
        cypher_gen=MagicMock(),
    )
    if tool_name not in registry._tools and not has_semantic_dependencies():
        pytest.skip(f"{tool_name} needs the vector backend, absent on a base install")
    return registry._tools[tool_name].input_schema


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
    summary: str = "ok",
):
    """An MCPToolsRegistry bound to a stub graph, with the real handler logic.

    The default `query_used` PROJECTS a qualified name, because that is what
    a scoped request needs in order to be judgeable. Tests that want the
    unjudgeable case pass their own.

    `summary` is a parameter so a test can assert the tool's own message
    SURVIVES rather than merely that some other message is absent. With it
    hardcoded, a translation-failure test could only check that the
    unscopeable text was missing -- which any replacement message satisfies,
    including a wrong one (reported on #1494).
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
            summary=summary,
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


class TestTheRestrictionMustBindTheAliasToThisProject:
    """A prefix operator on the alias is not a restriction TO the project.

    `_alias_is_restricted` checked only that `<alias>.qualified_name` was
    followed by a prefix operator, and `_restricts_to_project` accepted the
    project literal ANYWHERE in the body. So a vacuous predicate plus a
    tautology carrying the project name satisfied both, and the aggregate
    ran across every project (reported on #1494, reproduced by execution).
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_vacuous_prefix_with_a_tautology_is_refused(self) -> None:
        assert self._refused(
            "MATCH (n:Function) WHERE n.qualified_name STARTS WITH '' "
            f"AND '{ALPHA}' = '{ALPHA}' RETURN count(n) AS total"
        )

    def test_a_prefix_naming_another_project_is_refused(self) -> None:
        """The operand must be THIS project, not merely project-shaped."""
        assert self._refused(
            f'MATCH (n:Function) WHERE n.qualified_name STARTS WITH "{BETA}." '
            f"AND '{ALPHA}' = '{ALPHA}' RETURN count(n) AS total"
        )

    def test_a_genuine_binding_is_still_allowed(self) -> None:
        """THE CONTROL. The ordinary scoped count must keep working."""
        assert not self._refused(
            f'MATCH (n:Function) WHERE n.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(n) AS total"
        )


class TestTrailingClausesAreCutOnAnyWhitespace:
    """`ORDER BY` on the next line must not stay inside the projection.

    `CYPHER_POST_RETURN_KEYWORDS` were matched as literal single-spaced
    strings, so a NEWLINE before `ORDER BY` defeated the cut and the sort
    key stayed in the parsed projection. `RETURN n.name AS a` then looked
    like it projected a qualified name -- because `n.qualified_name` was
    sitting in the ORDER BY -- while the rows actually returned carry only
    `n.name`, which the row filter cannot attribute (reported on #1494).

    The aliased form is the one that leaks: `_every_term_is_plain` splits
    the term at ` AS `, so the trailing clause falls outside the part it
    checks and the term passes as a bare property read.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_newline_before_order_by_is_still_cut(self) -> None:
        assert self._refused("MATCH (n) RETURN n.name AS a\nORDER BY n.qualified_name")

    def test_a_newline_inside_order_by_is_still_cut(self) -> None:
        assert self._refused("MATCH (n) RETURN n.name AS a\nORDER\nBY n.qualified_name")

    def test_a_newline_before_skip_is_still_cut(self) -> None:
        assert self._refused(
            "MATCH (n) RETURN n.name AS a\nSKIP 1\nORDER BY n.qualified_name"
        )

    def test_a_newline_before_limit_is_still_cut(self) -> None:
        assert self._refused(
            "MATCH (n) RETURN n.name AS a\nLIMIT 5\nORDER BY n.qualified_name"
        )

    def test_a_real_projection_with_a_newline_order_by_is_allowed(self) -> None:
        """THE CONTROL. Cutting the clause must not lose genuine evidence."""
        assert not self._refused(
            "MATCH (n) RETURN n.qualified_name AS q\nORDER BY n.name"
        )

    def test_a_property_containing_a_keyword_is_not_a_trailing_clause(self) -> None:
        """The upper-bound guard: `n.reunion` contains the letters UNION.

        `" UNION"` carries no trailing space, so the leading whitespace is
        its only boundary. Without it the projection is truncated mid-word
        at `RE|UNION`, the qualified name that follows is lost, and a
        legitimate query is refused. Found by mutating the boundary away and
        seeing every test still pass.
        """
        assert not self._refused("MATCH (n) RETURN n.reunion, n.qualified_name")


class TestAForeignProjectLiteralAnywhereIsRefused:
    """Deliberately conservative, and pinned because nothing else pins it.

    `_restricts_to_project` requires EVERY project-shaped literal in the
    body to name the requested project. Since `_alias_is_restricted` began
    checking the operand, that is belt-and-braces: a query restricting the
    counted alias to another project is already refused there.

    What survives is a genuine over-restriction -- a query whose counted
    alias IS correctly bound to this project but which mentions another
    project's name in a non-restricting position. That is refused, which is
    the SAFE direction, but it was an unguarded decision: mutating the
    quantifier from `all` to `any` killed no test at all. Found by sweeping
    every quantifier in the module after one of them turned out to be wrong.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_foreign_literal_in_a_non_restricting_position_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            f"AND n.path CONTAINS '{BETA}.' RETURN count(n) AS total"
        )

    def test_a_counted_alias_bound_to_another_project_is_refused(self) -> None:
        """The case the literal rule was originally written for."""
        assert self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{BETA}.' "
            f"AND n.path CONTAINS '{ALPHA}.' RETURN count(n) AS total"
        )


class TestARegexRestrictionCannotBeShownPrefixLimited:
    """`=~ "alpha__aaaa1111|.*"` starts with the project name and matches all.

    `_alias_is_restricted` requires the operand to begin with the active
    project, which is sound for `STARTS WITH` and `=` on a literal. A REGEX
    is not a literal: an alternation after the prefix matches every foreign
    qualified name while satisfying the same textual check, and the scoped
    aggregate returns a scalar the row filter cannot attribute (#1494).

    Deciding whether an arbitrary pattern is prefix-limited is a language
    question this module has no business answering, so a regex predicate no
    longer authorises a scoped aggregate at all.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_regex_with_an_alternation_is_refused(self) -> None:
        assert self._refused(
            f'MATCH (n) WHERE n.qualified_name =~ "{ALPHA}|.*" RETURN count(n) AS total'
        )

    def test_even_a_faithful_regex_is_refused(self) -> None:
        """Refused as a CLASS, not by inspecting the pattern.

        This one really is limited to the project, and is still refused: the
        rule is that a regex cannot be shown limited, not that this pattern
        is bad. Pinned so the refusal reads as deliberate rather than as an
        accident of the alternation case above.
        """
        assert self._refused(
            f'MATCH (n) WHERE n.qualified_name =~ "^{ALPHA}\\\\." '
            "RETURN count(n) AS total"
        )

    def test_starts_with_is_still_allowed(self) -> None:
        """THE CONTROL. The shape the prompt mandates keeps working."""
        assert not self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )


class TestTheRestrictionMustBeAPlainConjunction:
    """A token blacklist tests spellings; the contract is a PROPERTY.

    Refusing `OR`/`NOT`/`XOR` enumerated the operators known to widen or
    invert a predicate. Cypher's boolean surface is larger than three
    keywords, and the Cypher here is LLM-GENERATED rather than emitted by a
    constrained builder, so the input language is not small enough for a
    blacklist to be sound. `CASE WHEN <pred> THEN true ELSE true END` and
    `coalesce(<pred>, true)` both contain none of the three tokens, both
    evaluate true for every row, and both were accepted (#1494).

    So the WHERE clause must now BE a conjunction of plain comparisons,
    rather than merely not contain known-bad ones. Anything else is refused
    unanalysed -- the same default-deny this module already applies to query
    structure, applied to the predicate.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_case_expression_returning_true_is_refused(self) -> None:
        assert self._refused(
            "MATCH (n) WHERE CASE WHEN n.qualified_name STARTS WITH "
            f"'{ALPHA}.' THEN true ELSE true END RETURN count(n) AS total"
        )

    def test_a_coalesce_defaulting_to_true_is_refused(self) -> None:
        assert self._refused(
            "MATCH (n) WHERE coalesce(n.qualified_name STARTS WITH "
            f"'{ALPHA}.', true) RETURN count(n) AS total"
        )

    def test_every_conjunct_must_be_plain_not_just_one(self) -> None:
        """Precedence makes `A OR B AND C` mean `A OR (B AND C)`.

        Splitting on AND yields `TRUE OR n.name = 'x'` and a real-looking
        restriction, so accepting the clause because SOME conjunct is plain
        admits a query that evaluates true for every row. Found by mutating
        `all` to `any` and seeing the whole suite stay green -- nothing else
        here distinguished the two.
        """
        assert self._refused(
            "MATCH (n) WHERE TRUE OR n.name = 'x' "
            f"AND n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )

    def test_a_plain_conjunction_is_still_allowed(self) -> None:
        """THE CONTROL. The shape the prompt actually mandates."""
        assert not self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "AND n.name = 'handler' RETURN count(n) AS total"
        )

    def test_a_single_predicate_is_still_allowed(self) -> None:
        assert not self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )


class TestAnAggregateWithNoBindableEntityIsRefused:
    """`count(*)` measures rows, and no alias means nothing to restrict.

    The restriction check ran `all(...)` over the set of counted aliases.
    `count(*)` captures no alias, so that set was EMPTY and `all()` over it
    is vacuously true -- the query passed while counting every node in every
    indexed project (reported on #1494).

    `RETURN n.qualified_name, count(*)` is a different shape and stays
    allowed: the count is grouped by a column the row filter can judge, so a
    foreign group is dropped whole.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_count_star_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(*) AS total"
        )

    def test_count_distinct_star_is_refused(self) -> None:
        """Found by sweeping the shape, not named in the report."""
        assert self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(DISTINCT *) AS total"
        )

    def test_a_bound_aggregate_is_still_allowed(self) -> None:
        """THE CONTROL. `count(n)` names an alias that IS restricted."""
        assert not self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )

    def test_a_grouped_count_star_is_also_refused(self) -> None:
        """This was a control asserting the OPPOSITE, and it was wrong.

        I argued a grouped `count(*)` was safe because the row filter can
        judge the grouping key. It cannot judge the COUNT. Grouping bounds
        which rows survive; `count(*)` counts MATCHES, and a match can
        involve an alias nothing restricts:

            MATCH (a)-[:CALLS]->(b) WHERE a.qualified_name STARTS WITH 'alpha.'
            RETURN a.qualified_name, count(*)

        returns an alpha-labelled row whose total includes beta callees --
        demonstrated by execution at total 2 against a both-endpoints-
        restricted control's total 1. The label passes the filter and carries
        a foreign magnitude past it (#1494).

        `count(*)` binds no alias, so its contributors cannot be enumerated
        and cannot be shown restricted. Refused wherever it appears in a
        scoped projection, not only in an all-aggregate one.
        """
        assert self._refused(
            "MATCH (n) RETURN n.qualified_name AS q, count(*) AS total"
        )

    def test_a_constant_string_operand_is_refused(self) -> None:
        """`count('x')` binds no alias, so its contributors are unchecked.

        The reported example was `count(true)`, which does NOT reproduce --
        `TRUE` uppercases into something the alias pattern matches, so it is
        collected as an alias, found unrestricted, and refused already. The
        CLASS is real even though that member is not: a QUOTED constant
        matches no identifier pattern, contributes nothing to the alias set,
        and left the contributor check with nothing to verify (#1494).

        So the rule is now what an operand must BE -- an alias this analysis
        can bind -- rather than a list of constant spellings to exclude.
        """
        assert self._refused(
            "MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name AS q, count('x') AS total"
        )

    def test_a_boolean_operand_is_refused(self) -> None:
        """Pinned even though it already passed, so it stays deliberate."""
        assert self._refused(
            "MATCH (a)-[:CALLS]->(b) RETURN a.qualified_name AS q, count(true) AS total"
        )

    def test_a_property_operand_is_still_allowed(self) -> None:
        """THE CONTROL. `count(n.qualified_name)` binds `n` and is checkable."""
        assert not self._refused(
            "MATCH (n) RETURN n.qualified_name AS q, count(n.name) AS total"
        )

    def test_a_grouped_count_over_a_restricted_alias_is_allowed(self) -> None:
        """THE CONTROL that replaces it: a NAMED alias can be checked."""
        assert not self._refused(
            "MATCH (n) RETURN n.qualified_name AS q, count(n) AS total"
        )


class TestADisjunctionCannotMakeTheRestrictionOptional:
    """`... STARTS WITH 'alpha.' OR TRUE` restricts nothing.

    `_alias_is_restricted` searches for the predicate as a SUBSTRING, so it
    is satisfied by a predicate that appears in the query without being
    required on every Boolean path. The aggregate then spans every project
    and its scalar result carries no qualified name for the row filter to
    reject (reported on #1494, reproduced end to end: `{'total': 2}` on a
    two-project graph while scoped to one).

    Refused rather than analysed. Deciding whether a project predicate is
    mandatory across arbitrary Boolean structure is a satisfiability
    question, and this module's stated rule is that an unanalysable shape is
    refused -- the same default-deny that governs `_UNANALYSABLE_RE`.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_or_true_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "OR TRUE RETURN count(n) AS total"
        )

    def test_or_a_tautology_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "OR 1=1 RETURN count(n) AS total"
        )

    def test_a_newline_separated_or_is_refused(self) -> None:
        """Cypher treats a newline as whitespace; a literal `" OR "` did not.

        The first version of this guard matched the spaced string, so the
        same disjunction written across two lines walked straight past it --
        the identical literal-whitespace defect that let a newline `ORDER BY`
        stay in the projection, reintroduced one function away (#1494).
        """
        assert self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.'\n"
            "OR TRUE RETURN count(n) AS total"
        )

    def test_a_tab_separated_or_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.'\t"
            "OR\tTRUE RETURN count(n) AS total"
        )

    def test_a_conjunction_is_still_allowed(self) -> None:
        """THE CONTROL. `AND` keeps the restriction mandatory, so it stays."""
        assert not self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "AND n.name = 'x' RETURN count(n) AS total"
        )

    def test_the_plain_restriction_is_still_allowed(self) -> None:
        assert not self._refused(
            f"MATCH (n:Function) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )

    def test_a_negated_predicate_is_refused(self) -> None:
        """`NOT (... STARTS WITH 'alpha.')` selects everything EXCEPT alpha.

        The predicate matches the textual check while its SENSE is inverted,
        so the restriction the guard found was the opposite of a restriction
        (reported on #1494).
        """
        assert self._refused(
            f"MATCH (n) WHERE NOT (n.qualified_name STARTS WITH '{ALPHA}.') "
            "RETURN count(n) AS total"
        )

    def test_a_negated_predicate_without_parentheses_is_refused(self) -> None:
        assert self._refused(
            f"MATCH (n) WHERE NOT n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )

    def test_an_xor_is_refused(self) -> None:
        """Found by sweeping the shape: XOR makes the predicate optional too."""
        assert self._refused(
            f"MATCH (n) WHERE n.qualified_name STARTS WITH '{ALPHA}.' "
            "XOR TRUE RETURN count(n) AS total"
        )

    def test_an_identifier_containing_or_is_not_a_disjunction(self) -> None:
        """The upper-bound guard: `n.coordinator` contains the letters OR.

        The body is uppercased before matching, so an unspaced `"OR" in body`
        refuses any query mentioning a property whose name happens to contain
        the substring. Found by mutating the spaced constant to an unspaced
        one and seeing every test still pass -- nothing here distinguished
        the operator from the letters until this case existed.
        """
        assert not self._refused(
            "MATCH (n:Function) WHERE n.coordinator = 'x' "
            f"AND n.qualified_name STARTS WITH '{ALPHA}.' "
            "RETURN count(n) AS total"
        )


class TestAPlainPropertyIsNotAttributionUnlessItIsTheQualifiedName:
    """Grouping by `n.name` does not let the row filter judge the row.

    `_entities_only_ever_aggregated` treated ANY plain property read as
    making an entity filterable, on the reasoning that it supplies a
    grouping column. Only a plain `qualified_name` does: `RETURN n.name,
    count(n.qualified_name)` returns a name and a number, neither of which
    the row filter can attribute, while the count spans every project.

    That was my own rule and this is the case it got wrong (#1494).
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_non_identifying_plain_property_does_not_excuse_the_aggregate(
        self,
    ) -> None:
        assert self._refused(
            "MATCH (n:Function) RETURN n.name, count(n.qualified_name) AS total"
        )

    def test_a_plain_qualified_name_still_excuses_the_aggregate(self) -> None:
        """THE CONTROL. A real grouping key keeps mixed aggregates usable."""
        assert not self._refused(
            "MATCH (n:Function) RETURN n.qualified_name AS q, count(n) AS total"
        )


class TestAnOpaqueValueCannotSmuggleAForeignProject:
    """A value the inspector cannot read must not be assumed harmless.

    `_names_another_project` understands strings and built-in containers.
    A graph driver's Node, Relationship or Path is none of those, so it fell
    through to `return False` and the row was KEPT -- then rendered with
    `str()`, printing another project's properties under an active scope
    (reported on #1494).

    Fails closed instead: unreadable means unattributable, which is the same
    rule the rest of this module already applies to queries. Plain scalars
    stay readable, so a genuine aggregate row is unaffected.
    """

    def test_an_opaque_object_carrying_a_foreign_name_is_dropped(self) -> None:
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        class _DriverNode:
            """Stands in for a driver entity: not a str, not a container."""

            def __init__(self, qn: str) -> None:
                self._properties = {"qualified_name": qn}

            def __str__(self) -> str:
                return str(self._properties)

        rows = [
            {"qualified_name": f"{ALPHA}.a.f", "node": _DriverNode(f"{BETA}.x.y")},
        ]

        assert scope_rows_to_project(rows, ALPHA) == []

    def test_a_genuine_aggregate_row_still_survives(self) -> None:
        """THE CONTROL. Scalars are readable and must not be failed closed."""
        from codebase_rag.tools.codebase_query import scope_rows_to_project

        rows = [{"total": 42, "ratio": 0.5, "ok": True, "missing": None}]

        assert scope_rows_to_project(rows, ALPHA) == rows


class TestACommentedPredicateDoesNotRestrictAnything:
    """A project predicate inside a comment is not executed by the database.

    The projection is analysed with comments and quoted strings blanked, but
    the RESTRICTION check received the original Cypher, so text the database
    never runs satisfied the scope guard. The aggregate then executed with no
    project predicate at all and returned a cross-project total (reported on
    #1494, reproduced by execution).

    Quoted strings must survive this stripping: the project literal
    legitimately lives inside quotes, so blanking them would refuse every
    correctly-scoped query instead.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_block_comment_predicate_is_refused(self) -> None:
        assert self._refused(
            f'MATCH (b) /* b.qualified_name STARTS WITH "{ALPHA}." */ '
            "RETURN count(b) AS total"
        )

    def test_a_line_comment_predicate_is_refused(self) -> None:
        assert self._refused(
            f'MATCH (b) // b.qualified_name STARTS WITH "{ALPHA}."\n'
            "RETURN count(b) AS total"
        )

    def test_a_genuine_predicate_is_still_allowed(self) -> None:
        """THE CONTROL. Stripping must not refuse a real restriction."""
        assert not self._refused(
            f'MATCH (b) WHERE b.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
        )

    def test_a_double_slash_inside_a_string_is_not_a_comment(self) -> None:
        """`"http://..."` must not be mistaken for a line comment.

        Blanking from the `//` would swallow the rest of the line including
        the genuine predicate, refusing a correct query.
        """
        assert not self._refused(
            f'MATCH (b) WHERE b.path STARTS WITH "http://x" '
            f'AND b.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
        )


class TestATranslationFailureIsNotReportedAsUnscopeable:
    """A query that was never generated cannot be judged unscopeable.

    `QUERY_NOT_AVAILABLE` has no RETURN clause, so it fails the evidence
    check like any other unjudgeable query -- and the handler replaced the
    real translation error with "this query cannot be scoped". The caller
    then sees a scoping complaint about a query that does not exist, and the
    actual failure is discarded. Only reachable on a SCOPED request, which is
    why the unscoped path never showed it (reported on #1494).
    """

    @pytest.mark.asyncio
    async def test_the_underlying_error_survives(self) -> None:
        """Asserts the real message is PRESENT, not that another is absent.

        An absence assertion is satisfied by every other outcome, including
        a regression that replaces the translation failure with some third
        error. So the fixture carries a representative message and the test
        requires it to arrive intact.
        """
        translation_error = "Cypher generation failed: unparseable request"
        handler = _handler_returning(
            [], query_used=cs.QUERY_NOT_AVAILABLE, summary=translation_error
        )

        result = await handler.query_code_graph("something unparseable", project=ALPHA)

        assert result.get("summary") == translation_error
        assert result.get("query_used") == cs.QUERY_NOT_AVAILABLE

    @pytest.mark.asyncio
    async def test_an_empty_query_is_also_not_a_scoping_complaint(self) -> None:
        translation_error = "Cypher generation returned nothing"
        handler = _handler_returning([], query_used="", summary=translation_error)

        result = await handler.query_code_graph("something unparseable", project=ALPHA)

        assert result.get("summary") == translation_error
        assert result.get("query_used") == ""


class TestPropertyAggregatesAreAttributed:
    """An aggregate over a PROPERTY measures its entity, exactly as a bare one does.

    `_every_projected_entity_is_attributable` registers an aggregated entity
    only when it appears as a BARE identifier -- `count(b)`. The property form
    `count(b.qualified_name)` reaches the same function as an ordinary property
    read, satisfies "this entity projects its own qualified name", and returns
    early, so the restriction check below is never reached for it.

    The two checks answer different questions, and only one of them is the
    right question for an aggregate. Attributability asks "does this row say
    who it is about"; restriction asks "is this entity confined to my
    project". For a projection of NAMES those coincide. An aggregate returns
    no names at all, so attributability is satisfied trivially while the
    MAGNITUDE still spans every indexed project.
    """

    def _refused(self, query: str) -> bool:
        from codebase_rag.tools.codebase_query import requires_project_evidence

        return not requires_project_evidence(query, ALPHA)

    def test_a_property_aggregate_over_an_unrestricted_alias_is_refused(self) -> None:
        assert self._refused(
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b.qualified_name) AS total"
        )

    def test_a_distinct_property_aggregate_is_refused_too(self) -> None:
        """DISTINCT sits INSIDE the aggregate, so it must not change the answer."""
        assert self._refused(
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(DISTINCT b.qualified_name) AS total"
        )

    def test_a_collect_over_a_property_is_refused(self) -> None:
        """`collect` returns the VALUES, not merely a magnitude.

        The worst of the three: it hands back other projects' qualified names
        outright rather than a count of them.
        """
        assert self._refused(
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN collect(b.qualified_name) AS names"
        )

    def test_a_mixed_projection_is_refused_when_any_alias_is_unrestricted(self) -> None:
        """The too-LITTLE twin of the control below.

        A fix that inspects only the first projection term, or that stops at
        the first restricted alias it finds, refuses all three leaks above and
        still permits this one. Nothing else here would notice.
        """
        assert self._refused(
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(a.qualified_name), count(b.qualified_name)"
        )

    def test_a_property_aggregate_over_the_restricted_alias_is_allowed(self) -> None:
        """THE CONTROL, and the too-MUCH guard.

        Identical in shape to the refusals above except that the counted alias
        IS the one the WHERE restricts. A fix that simply refuses every
        property aggregate passes all four leak tests while breaking
        legitimate scoped counting, and would look correct.
        """
        assert not self._refused(
            f'MATCH (a) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(a.qualified_name) AS total"
        )

    def test_the_bare_aggregate_case_still_behaves(self) -> None:
        """Regression pin: the original bare-`count(b)` finding stays fixed."""
        assert self._refused(
            f'MATCH (a),(b) WHERE a.qualified_name STARTS WITH "{ALPHA}." '
            "RETURN count(b) AS total"
        )
