"""Calls between SQL routines become CALLS edges (issue #2449).

tree-sitter-sql names an `invocation`'s callee through an unnamed
`object_reference` child, and names a `create_function`'s body through an
unnamed `function_body` child. The call pass looked for `function`/`name`/
`body` fields only, so every SQL call site was dropped and no routine ever
called another, although the language matrix advertises "invocations between
routines".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater

tree_sitter_sql = pytest.importorskip("tree_sitter_sql")

_PROJECT = "sqlcalls"

# The issue's reproduction, verbatim.
_ISSUE_FNS = """\
CREATE FUNCTION tax(amount NUMERIC) RETURNS NUMERIC AS $$
  SELECT amount * 0.2;
$$ LANGUAGE sql;

CREATE FUNCTION gross(amount NUMERIC) RETURNS NUMERIC AS $$
  SELECT amount + tax(amount);
$$ LANGUAGE sql;

CREATE FUNCTION billing.net(amount NUMERIC) RETURNS NUMERIC AS $$
  SELECT gross(amount) - billing.fee(amount);
$$ LANGUAGE sql;

CREATE FUNCTION billing.fee(amount NUMERIC) RETURNS NUMERIC AS $$
  SELECT 1;
$$ LANGUAGE sql;

CREATE FUNCTION in_where() RETURNS SETOF int AS $$
  SELECT id FROM t WHERE tax(id) > 1;
$$ LANGUAGE sql;
"""


def _qn(rest: str) -> str:
    return f"{_PROJECT}.{rest}"


def _index(tmp_path: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    project = tmp_path / _PROJECT
    for rel_path, source in files.items():
        target = project / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    create_and_run_updater(project, mock_ingestor, skip_if_missing="sql")


def _calls(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS
    }


def _resolutions(mock_ingestor: MagicMock) -> dict[tuple[str, str], str]:
    return {
        (str(c.args[0][2]), str(c.args[2][2])): str(
            c.kwargs.get("properties", {}).get(cs.KEY_RESOLUTION)
        )
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS
    }


def _callees_of(mock_ingestor: MagicMock, caller: str) -> set[str]:
    return {callee for src, callee in _calls(mock_ingestor) if src == caller}


def _invocation_nodes(source: str) -> list[Node]:
    tree = Parser(Language(tree_sitter_sql.language())).parse(source.encode())
    found: list[Node] = []

    def walk(node: Node) -> None:
        if node.type == cs.TS_SQL_INVOCATION:
            found.append(node)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return found


class TestInvocationName:
    def test_names_the_callee_with_its_schema(self) -> None:
        from codebase_rag.language_spec import sql_object_reference_name

        names = [
            sql_object_reference_name(node)
            for node in _invocation_nodes(
                'SELECT tax(1), billing.fee(2), "App"."F"(3);'
            )
        ]
        assert names == ["tax", "billing.fee", "App.F"]

    def test_unquoted_callee_folds_like_a_definition(self) -> None:
        from codebase_rag.language_spec import sql_object_reference_name

        (node,) = _invocation_nodes("SELECT Billing.TAX(1);")
        assert sql_object_reference_name(node) == "billing.tax"


class TestIssueReproduction:
    def test_bare_call_links_the_routines(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(tmp_path, mock_ingestor, {"db/fns.sql": _ISSUE_FNS})
        assert (_qn("db.fns.gross"), _qn("db.fns.tax")) in _calls(mock_ingestor)

    def test_schema_qualified_call_links_the_schema_routine(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(tmp_path, mock_ingestor, {"db/fns.sql": _ISSUE_FNS})
        assert _callees_of(mock_ingestor, _qn("db.fns.billing.net")) == {
            _qn("db.fns.gross"),
            _qn("db.fns.billing.fee"),
        }

    def test_call_inside_where_links_the_routines(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(tmp_path, mock_ingestor, {"db/fns.sql": _ISSUE_FNS})
        assert _callees_of(mock_ingestor, _qn("db.fns.in_where")) == {_qn("db.fns.tax")}

    def test_all_expected_edges_and_nothing_else(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # The body's calls belong to the routine: the module running the
        # CREATE statements calls nothing, and a module-attributed copy of
        # each edge would make every routine look called at load time.
        _index(tmp_path, mock_ingestor, {"db/fns.sql": _ISSUE_FNS})
        assert _calls(mock_ingestor) == {
            (_qn("db.fns.gross"), _qn("db.fns.tax")),
            (_qn("db.fns.billing.net"), _qn("db.fns.gross")),
            (_qn("db.fns.billing.net"), _qn("db.fns.billing.fee")),
            (_qn("db.fns.in_where"), _qn("db.fns.tax")),
        }

    def test_unique_target_is_an_exact_edge(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(tmp_path, mock_ingestor, {"db/fns.sql": _ISSUE_FNS})
        assert (
            _resolutions(mock_ingestor)[(_qn("db.fns.gross"), _qn("db.fns.tax"))]
            == cs.EdgeResolution.EXACT
        )


class TestRoutineResolution:
    def test_call_reaches_a_routine_in_another_file(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # SQL has no imports: every routine of the database is in scope.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/a.sql": "CREATE FUNCTION tax(x INT) RETURNS INT "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n",
                "db/b.sql": "CREATE FUNCTION gross(x INT) RETURNS INT "
                "AS $$ SELECT tax(x); $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == {(_qn("db.b.gross"), _qn("db.a.tax"))}

    def test_unquoted_callee_folds_to_lowercase(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # PostgreSQL folds TAX to tax, as it does on the definition side.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION tax(x INT) RETURNS INT "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION gross(x INT) RETURNS INT "
                "AS $$ SELECT TAX(x); $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == {(_qn("db.fns.gross"), _qn("db.fns.tax"))}

    def test_quoted_callee_matches_a_quoted_definition(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": 'CREATE FUNCTION "App"."MyFn"(x INT) RETURNS INT '
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                'AS $$ SELECT "App"."MyFn"(1); $$ LANGUAGE sql;\n',
            },
        )
        assert _calls(mock_ingestor) == {(_qn("db.fns.caller"), _qn("db.fns.App.MyFn"))}

    def test_call_reaches_every_overload(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Overloads differ by argument types the call site does not spell,
        # so each one stays reachable.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION tax(x NUMERIC) RETURNS NUMERIC "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION tax(x INT) RETURNS INT "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION gross(x INT) RETURNS INT "
                "AS $$ SELECT tax(x); $$ LANGUAGE sql;\n",
            },
        )
        callees = _callees_of(mock_ingestor, _qn("db.fns.gross"))
        assert len(callees) == 2
        assert _qn("db.fns.tax") in callees
        assert {
            _resolutions(mock_ingestor)[(_qn("db.fns.gross"), callee)]
            for callee in callees
        } == {cs.EdgeResolution.OVERLOAD}

    def test_qualified_call_reaches_every_overload_of_its_schema(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # The second overload registers as a duplicate variant whose qn no
        # longer ends in `.billing.fee`, so a plain suffix scan misses it.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION billing.fee(x NUMERIC) RETURNS NUMERIC "
                "AS $$ SELECT 1; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION billing.fee(x INT) RETURNS INT "
                "AS $$ SELECT 2; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION audit.fee(x INT) RETURNS INT "
                "AS $$ SELECT 3; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                "AS $$ SELECT billing.fee(1); $$ LANGUAGE sql;\n",
            },
        )
        callees = _callees_of(mock_ingestor, _qn("db.fns.caller"))
        assert len(callees) == 2
        assert _qn("db.fns.billing.fee") in callees
        assert not any(".audit." in callee for callee in callees)

    def test_unqualified_call_reaches_every_schema(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # Without a qualifier the runtime search_path picks the schema, so
        # every same-named routine is a candidate; the edges say they are a
        # name-only match.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION billing.fee(x INT) RETURNS INT "
                "AS $$ SELECT 1; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION audit.fee(x INT) RETURNS INT "
                "AS $$ SELECT 2; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                "AS $$ SELECT fee(1); $$ LANGUAGE sql;\n",
            },
        )
        edges = {
            (_qn("db.fns.caller"), _qn("db.fns.billing.fee")),
            (_qn("db.fns.caller"), _qn("db.fns.audit.fee")),
        }
        assert _calls(mock_ingestor) == edges
        resolutions = _resolutions(mock_ingestor)
        assert {resolutions[edge] for edge in edges} == {cs.EdgeResolution.HEURISTIC}


class TestNoFalseEdges:
    def test_qualified_call_does_not_cross_schemas(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # billing.fee names one routine; audit.fee merely shares its name.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION billing.fee(x INT) RETURNS INT "
                "AS $$ SELECT 1; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION audit.fee(x INT) RETURNS INT "
                "AS $$ SELECT 2; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                "AS $$ SELECT billing.fee(1); $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == {
            (_qn("db.fns.caller"), _qn("db.fns.billing.fee"))
        }

    def test_qualified_call_to_a_missing_schema_links_nothing(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION billing.levy(x INT) RETURNS INT "
                "AS $$ SELECT 1; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                "AS $$ SELECT other.levy(1); $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == set()

    def test_file_named_like_the_schema_is_not_the_schema(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # `fee` in billing.sql lives in the default schema; its qn merely
        # ends in `billing.fee` because of the FILE name.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/billing.sql": "CREATE FUNCTION fee(x INT) RETURNS INT "
                "AS $$ SELECT 1; $$ LANGUAGE sql;\n",
                "db/caller.sql": "CREATE FUNCTION caller() RETURNS INT "
                "AS $$ SELECT billing.fee(1); $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == set()

    def test_quoted_callee_keeps_its_case(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # "Tax" and tax are different routines in PostgreSQL.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION tax(x INT) RETURNS INT "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "CREATE FUNCTION caller() RETURNS INT "
                'AS $$ SELECT "Tax"(1); $$ LANGUAGE sql;\n',
            },
        )
        assert _calls(mock_ingestor) == set()

    def test_builtin_calls_link_nothing(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION caller() RETURNS INT AS $$ "
                "SELECT count(*) + coalesce(1, 2) + extract(day FROM now())::int "
                "FROM t; $$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == set()

    def test_sql_call_never_binds_a_function_of_another_language(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # The database cannot run app/util.py: neither a bare nor a
        # qualified spelling may reach the Python function.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "app/util.py": "def helper(n):\n    return n\n\n"
                "def count(n):\n    return n\n",
                "db/fns.sql": "CREATE FUNCTION caller() RETURNS INT AS $$ "
                "SELECT helper(1) + util.helper(2) + count(*) FROM t; "
                "$$ LANGUAGE sql;\n",
            },
        )
        assert _calls(mock_ingestor) == set()

    def test_top_level_statement_stays_module_attributed(
        self, tmp_path: Path, mock_ingestor: MagicMock
    ) -> None:
        # A call OUTSIDE any routine body runs when the script runs.
        _index(
            tmp_path,
            mock_ingestor,
            {
                "db/fns.sql": "CREATE FUNCTION tax(x INT) RETURNS INT "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
                "SELECT tax(2);\n",
            },
        )
        assert _calls(mock_ingestor) == {(_qn("db.fns"), _qn("db.fns.tax"))}
