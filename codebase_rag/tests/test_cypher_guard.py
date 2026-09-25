from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag.graph_dialects import DIALECT_MEMGRAPH, get_dialect
from codebase_rag.services.cypher_guard import (
    check_memgraph_plan,
    is_allowed_procedure,
    mask_literals_and_comments,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import (
    _validate_call_procedures,
    _validate_cypher_read_only,
)
from codebase_rag.types_defs import PropertyDict


class TestMaskLiteralsAndComments:
    def test_string_literal_is_blanked(self) -> None:
        assert (
            mask_literals_and_comments("WHERE f.name = 'delete' RETURN f")
            == "WHERE f.name = '' RETURN f"
        )

    def test_double_quoted_literal_is_blanked(self) -> None:
        assert mask_literals_and_comments('RETURN "SET x"') == "RETURN ''"

    def test_escaped_quote_does_not_end_the_literal(self) -> None:
        assert mask_literals_and_comments(r"RETURN 'a\' DELETE n' AS x") == (
            "RETURN '' AS x"
        )

    def test_backtick_identifier_is_unquoted(self) -> None:
        assert (
            mask_literals_and_comments("CALL `mg.create_module_file`('a')")
            == "CALL mg.create_module_file('')"
        )

    def test_doubled_backtick_is_a_literal_backtick(self) -> None:
        assert mask_literals_and_comments("RETURN `a``b`") == "RETURN a`b"

    def test_comments_become_spaces(self) -> None:
        assert (
            mask_literals_and_comments("CALL/*x*/mg.x() // tail\nRETURN 1")
            == "CALL mg.x()  \nRETURN 1"
        )

    def test_comment_markers_inside_a_literal_are_not_comments(self) -> None:
        assert mask_literals_and_comments("RETURN '//' , 1") == "RETURN '' , 1"

    @pytest.mark.parametrize(
        "query",
        ["RETURN 'unterminated DELETE", "RETURN 1 /* DELETE", "CALL `mg.x"],
    )
    def test_unterminated_construct_is_left_visible(self, query: str) -> None:
        # The engine rejects these anyway; masking them could hide a keyword.
        assert mask_literals_and_comments(query) == query


class TestIsAllowedProcedure:
    @pytest.mark.parametrize(
        "name", ["pagerank.get", "nxalg.simple_cycles", "graph_util.ancestors"]
    )
    def test_read_only_procedures_are_allowed(self, name: str) -> None:
        assert is_allowed_procedure(name)

    @pytest.mark.parametrize("name", sorted(cs.CYPHER_DENIED_PROCEDURES))
    def test_denied_procedures_are_refused_despite_their_prefix(
        self, name: str
    ) -> None:
        assert is_allowed_procedure(name) is False

    @pytest.mark.parametrize(
        "name", ["mg.create_module_file", "export_util.json", "SCHEMA.assert"]
    )
    def test_procedures_outside_the_families_are_refused(self, name: str) -> None:
        assert is_allowed_procedure(name) is False


class TestTextValidatorsSeeThroughQuoting:
    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (n:Project) CALL `mg.create_module_file`('p.py', 'x') "
            "YIELD path RETURN path;",
            "MATCH (n) CALL /*c*/ export_util.json('/tmp/x.json');",
            "MATCH (n) CALL // c\n export_util.json('/tmp/x.json');",
            "MATCH (n) CALL `mg`.`create_module_file`('p.py', 'x') YIELD path RETURN path;",
            "MATCH (n) CALL mg . create_module_file('p.py', 'x') YIELD path RETURN path;",
            "MATCH (n) CALL schema.assert({}, {}, {}, true) YIELD label RETURN label;",
            "MATCH (n) WITH collect(n) AS ns CALL graph_util.chain_nodes(ns, 'X') "
            "YIELD connections RETURN 1;",
        ],
    )
    def test_hidden_or_writing_procedure_is_rejected(self, query: str) -> None:
        with pytest.raises(ex.LLMGenerationError, match="outside the read-only"):
            _validate_call_procedures(query)

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (f:Function) WHERE f.name = 'delete' RETURN f;",
            "MATCH (f:Function) WHERE f.name STARTS WITH 'set' RETURN f;",
            'MATCH (f:Function) WHERE f.name = "create_user" RETURN f;',
            "MATCH (f:Function) // find merge helpers\nRETURN f;",
        ],
    )
    def test_keyword_inside_a_literal_or_comment_is_not_a_write(
        self, query: str
    ) -> None:
        _validate_cypher_read_only(query)

    def test_keyword_outside_a_literal_is_still_rejected(self) -> None:
        with pytest.raises(ex.LLMGenerationError, match="DELETE"):
            _validate_cypher_read_only("MATCH (n) WHERE n.x = 'a' DETACH DELETE n;")


# Plans as memgraph 3.3 prints them for EXPLAIN.
_READ_PLAN = [
    " * Limit",
    " * OrderBy {n, count(m)}",
    " * Produce {n, count(m)}",
    " * Aggregate {COUNT-1} {n}",
    " * ExpandVariable (n)-[r]->(m)",
    " * Filter {n.name}",
    " * ScanAll (n)",
    " * Once",
]
_PAGERANK_PLAN = [
    " * Produce {node, rank}",
    " * CallProcedure<pagerank.get> {node, rank}",
    " * Once",
]
_SUBQUERY_WRITE_PLAN = [
    " * Produce {x}",
    " * Apply",
    " |\\ ",
    " | * Produce {x}",
    " | * CreateNode",
    " | * Produce {n}",
    " | * Once",
    " * ScanAll (n)",
    " * Once",
]


class TestCheckMemgraphPlan:
    def test_read_plan_passes(self) -> None:
        check_memgraph_plan(_READ_PLAN, "q")

    def test_allowed_procedure_passes(self) -> None:
        check_memgraph_plan(_PAGERANK_PLAN, "q")

    @pytest.mark.parametrize(
        "operator",
        [
            "CreateNode",
            "CreateExpand (a)-[anon3:R]->(b)",
            "SetProperty",
            "SetProperties",
            "SetLabels",
            "RemoveProperty",
            "RemoveLabels",
            "Delete",
            "Merge",
            "Foreach",
            "LoadCsv {r}",
        ],
    )
    def test_write_operator_is_refused(self, operator: str) -> None:
        with pytest.raises(ex.ReadOnlyQueryError, match="write operation"):
            check_memgraph_plan([" * EmptyResult", f" * {operator}", " * Once"], "q")

    def test_write_inside_a_subquery_branch_is_refused(self) -> None:
        with pytest.raises(ex.ReadOnlyQueryError, match="CreateNode"):
            check_memgraph_plan(_SUBQUERY_WRITE_PLAN, "q")

    @pytest.mark.parametrize(
        "name", ["mg.create_module_file", "graph_util.chain_nodes", "schema.assert"]
    )
    def test_disallowed_procedure_is_refused(self, name: str) -> None:
        with pytest.raises(ex.ReadOnlyQueryError, match=name):
            check_memgraph_plan([f" * CallProcedure<{name}> {{path}}"], "q")


class TestFetchReadOnlyOnMemgraph:
    def _ingestor(
        self, monkeypatch: pytest.MonkeyPatch, plan: list[str]
    ) -> tuple[MemgraphIngestor, MagicMock]:
        executed = MagicMock()

        def fake_execute(
            self: MemgraphIngestor, query: str, params: PropertyDict | None = None
        ) -> list[dict[str, str]]:
            executed(query)
            if query.startswith(cs.CYPHER_EXPLAIN_PREFIX):
                return [{"QUERY PLAN": row} for row in plan]
            return [{"name": "f"}]

        monkeypatch.setattr(MemgraphIngestor, "_execute_query", fake_execute)
        ingestor = MemgraphIngestor(
            host="localhost", port=7687, dialect=get_dialect(DIALECT_MEMGRAPH)
        )
        return ingestor, executed

    def test_read_query_is_planned_then_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ingestor, executed = self._ingestor(monkeypatch, _READ_PLAN)
        assert ingestor.fetch_read_only("MATCH (n) RETURN n") == [{"name": "f"}]
        queries = [call.args[0] for call in executed.call_args_list]
        assert len(queries) == 2
        assert queries[0].startswith(cs.CYPHER_EXPLAIN_PREFIX)
        assert queries[1] == queries[0][len(cs.CYPHER_EXPLAIN_PREFIX) :]

    def test_writing_query_is_never_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ingestor, executed = self._ingestor(monkeypatch, _SUBQUERY_WRITE_PLAN)
        with pytest.raises(ex.ReadOnlyQueryError):
            ingestor.fetch_read_only(
                "MATCH (n) CALL { CREATE (x:Y) RETURN x } RETURN x"
            )
        executed.assert_called_once()
        assert executed.call_args.args[0].startswith(cs.CYPHER_EXPLAIN_PREFIX)
