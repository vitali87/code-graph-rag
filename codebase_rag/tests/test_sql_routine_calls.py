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
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.conftest import create_and_run_updater
from codebase_rag.tests.test_incremental_added_dependents import _add_after_cache
from codebase_rag.tests.test_incremental_deleted_dependents import (
    _index as _index_into,
)
from codebase_rag.tests.test_incremental_deleted_dependents import _materialise
from evals.cgr_graph import _StatefulIngestor

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


_CALLER_QN = "proj.db.caller.caller"


def _routine(name: str) -> str:
    return (
        f"CREATE FUNCTION {name}(x INT) RETURNS INT AS $$ SELECT x; $$ LANGUAGE sql;\n"
    )


def _caller(call: str) -> str:
    return (
        f"CREATE FUNCTION caller() RETURNS INT AS $$ SELECT {call}(1); "
        "$$ LANGUAGE sql;\n"
    )


def _caller_edges(store: _StatefulIngestor) -> dict[str, str]:
    # callee qn -> resolution label of the caller's CALLS edges
    return {
        str(edge[4]): str(store.props_for(edge).get(cs.KEY_RESOLUTION))
        for edge in store.edges
        if edge[1] == _CALLER_QN and edge[2] == cs.RelationshipType.CALLS
    }


def _clean_edges(tmp_path: Path, files: dict[str, str]) -> dict[str, str]:
    root = tmp_path / "clean" / "proj"
    root.parent.mkdir()
    _materialise(root, files)
    store = _StatefulIngestor()
    _index_into(store, root, cs.SupportedLanguage.SQL, force=True)
    return _caller_edges(store)


class _WaiterSpy:
    """The files the added-definition lookup sends back for a re-parse."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.returned: list[str] = []
        original = GraphUpdater._unresolved_reference_waiters

        def spy(
            updater: GraphUpdater,
            added: list[tuple[str, bytes]],
            modified: list[tuple[str, bytes]] | None = None,
        ) -> list[str]:
            keys = original(updater, added, modified)
            self.returned.extend(keys)
            return keys

        monkeypatch.setattr(GraphUpdater, "_unresolved_reference_waiters", spy)


class TestIncrementalMatchesCleanIndex:
    """An unchanged caller has no edge into a file that ADDS a routine it can
    reach, so only the waiter list (issue #1568) can send it back for a
    re-parse; the incremental graph must equal a clean index of the tree."""

    def _sync(
        self,
        tmp_path: Path,
        before: dict[str, str],
        changes: dict[str, str | None],
    ) -> tuple[dict[str, str], dict[str, str]]:
        root = tmp_path / "proj"
        _materialise(root, before)
        store = _StatefulIngestor()
        _index_into(store, root, cs.SupportedLanguage.SQL, force=True)
        for rel, text in changes.items():
            if text is None:
                (root / rel).unlink()
            else:
                _add_after_cache(root, rel, text)
        _index_into(store, root, cs.SupportedLanguage.SQL, force=False)
        after = {
            rel: text for rel, text in {**before, **changes}.items() if text is not None
        }
        return _caller_edges(store), _clean_edges(tmp_path, after)

    def test_added_schema_routine_fans_out_an_unqualified_call(
        self, tmp_path: Path
    ) -> None:
        incremental, clean = self._sync(
            tmp_path,
            {"db/b.sql": _routine("billing.fee"), "db/caller.sql": _caller("fee")},
            {"db/a.sql": _routine("audit.fee")},
        )
        assert clean == {
            "proj.db.b.billing.fee": cs.EdgeResolution.HEURISTIC,
            "proj.db.a.audit.fee": cs.EdgeResolution.HEURISTIC,
        }
        assert incremental == clean

    def test_modified_file_gaining_a_schema_routine_fans_out(
        self, tmp_path: Path
    ) -> None:
        # The edited file held nothing the caller reached, so no edge leads
        # the incremental pass from it to the caller.
        incremental, clean = self._sync(
            tmp_path,
            {
                "db/b.sql": _routine("billing.fee"),
                "db/other.sql": _routine("levy"),
                "db/caller.sql": _caller("fee"),
            },
            {"db/other.sql": _routine("levy") + _routine("audit.fee")},
        )
        assert set(clean) == {"proj.db.b.billing.fee", "proj.db.other.audit.fee"}
        assert incremental == clean

    def test_added_uppercase_routine_reaches_a_lowercase_caller(
        self, tmp_path: Path
    ) -> None:
        # The added file spells AUDIT.FEE; PostgreSQL folds it to the
        # audit.fee the caller's `fee` can reach, and the waiter key must fold
        # the same way or the caller is never revisited.
        incremental, clean = self._sync(
            tmp_path,
            {"db/b.sql": _routine("billing.fee"), "db/caller.sql": _caller("fee")},
            {"db/a.sql": _routine("AUDIT.FEE")},
        )
        assert set(clean) == {"proj.db.b.billing.fee", "proj.db.a.audit.fee"}
        assert incremental == clean

    def test_added_same_schema_routine_reaches_a_qualified_call(
        self, tmp_path: Path
    ) -> None:
        # A second file defining billing.fee (an overload) is one more
        # routine a qualified billing.fee(...) can run.
        incremental, clean = self._sync(
            tmp_path,
            {
                "db/b.sql": _routine("billing.fee"),
                "db/caller.sql": _caller("billing.fee"),
            },
            {
                "db/b2.sql": "CREATE FUNCTION billing.fee(x NUMERIC) RETURNS NUMERIC "
                "AS $$ SELECT x; $$ LANGUAGE sql;\n"
            },
        )
        assert set(clean) == {"proj.db.b.billing.fee", "proj.db.b2.billing.fee"}
        assert incremental == clean

    def test_removed_schema_routine_returns_to_one_exact_edge(
        self, tmp_path: Path
    ) -> None:
        # The caller HAS an edge into the deleted file, so the inbound-edge
        # dependents re-parse it.
        incremental, clean = self._sync(
            tmp_path,
            {
                "db/a.sql": _routine("audit.fee"),
                "db/b.sql": _routine("billing.fee"),
                "db/caller.sql": _caller("fee"),
            },
            {"db/a.sql": None},
        )
        assert clean == {"proj.db.b.billing.fee": cs.EdgeResolution.EXACT}
        assert incremental == clean

    def test_qualified_call_is_not_revisited_for_another_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # billing.fee(...) can never reach audit.fee, so adding it must
        # neither re-parse the caller nor change its edge.
        spy = _WaiterSpy(monkeypatch)
        incremental, clean = self._sync(
            tmp_path,
            {
                "db/b.sql": _routine("billing.fee"),
                "db/caller.sql": _caller("billing.fee"),
            },
            {"db/a.sql": _routine("audit.fee")},
        )
        assert "db/caller.sql" not in spy.returned
        assert clean == {"proj.db.b.billing.fee": cs.EdgeResolution.EXACT}
        assert incremental == clean

    def test_unrelated_routine_does_not_revisit_callers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _WaiterSpy(monkeypatch)
        incremental, clean = self._sync(
            tmp_path,
            {"db/b.sql": _routine("billing.fee"), "db/caller.sql": _caller("fee")},
            {"db/a.sql": _routine("audit.levy")},
        )
        assert "db/caller.sql" not in spy.returned
        assert clean == {"proj.db.b.billing.fee": cs.EdgeResolution.EXACT}
        assert incremental == clean

    def test_caller_records_each_routine_name_it_wrote(self, tmp_path: Path) -> None:
        # Resolved or not, the normalized name stays on the waiter list: a
        # later routine of that name changes what a clean index links.
        root = tmp_path / "proj"
        _materialise(
            root,
            {
                "db/b.sql": _routine("billing.fee"),
                "db/caller.sql": "CREATE FUNCTION caller() RETURNS INT AS $$ "
                "SELECT FEE(1) + Billing.Fee(2) + count(*) FROM t; $$ LANGUAGE sql;\n",
            },
        )
        store = _StatefulIngestor()
        _index_into(store, root, cs.SupportedLanguage.SQL, force=True)
        module = store.nodes[(cs.NodeLabel.MODULE.value, "proj.db.caller")]
        assert module[cs.KEY_UNRESOLVED_REFERENCES] == ["billing.fee", "count", "fee"]
