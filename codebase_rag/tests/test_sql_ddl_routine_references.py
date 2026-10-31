"""A SQL routine is a root, and DDL that names one references it (issue #3181).

A stored function is called by name from any client, so it is public API,
but every SQL `Function` was stored `is_exported: false` and `dead-code`
reported nearly all of them (TimescaleDB: 313 of 338). And the DDL that
wires a routine into the schema (a trigger's `EXECUTE FUNCTION f()`, an
aggregate's `SFUNC = f`, a cast's `WITH FUNCTION f`, a type's `INPUT = f`,
an FDW's `HANDLER f`) recorded no edge to it, so `callers` was empty. Most
of those statements do not even parse in tree-sitter-sql, so the clauses
are read lexically, outside comments, strings and routine bodies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.tests.test_incremental_added_dependents import _add_after_cache
from codebase_rag.tests.test_incremental_deleted_dependents import (
    _index as _index_into,
)
from codebase_rag.tests.test_incremental_deleted_dependents import _materialise
from codebase_rag.types_defs import PropertyParams, PropertyValue, ResultRow
from evals.cgr_graph import _StatefulIngestor

pytest.importorskip("tree_sitter_sql")

PROJECT = "sqlddl"

# The issue's reproduction, verbatim.
_SCHEMA = """\
CREATE TABLE items (id integer PRIMARY KEY, n integer, updated_at timestamptz);

-- API: called by the application (SELECT api_get_item(1))
CREATE FUNCTION api_get_item(item_id integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT n FROM items WHERE id = item_id $$;

-- trigger function, run by the trigger below
CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

CREATE TRIGGER items_touch BEFORE UPDATE ON items
    FOR EACH ROW EXECUTE FUNCTION touch();

-- aggregate state function, run by my_sum()
CREATE FUNCTION sum_sfunc(state integer, v integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT state + v $$;

CREATE AGGREGATE my_sum(integer) (SFUNC = sum_sfunc, STYPE = integer);
"""

# One routine per DDL clause, defined in routines.sql and named only by
# the DDL in wiring.sql, so every edge below crosses files.
_ROUTINES = """\
CREATE FUNCTION audit.log_row() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;
CREATE FUNCTION process_ddl_event() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN RETURN; END; $$;
CREATE FUNCTION first_fin(internal) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION first_combine(internal, internal) RETURNS internal LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION first_sfunc(internal, integer) RETURNS internal LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION "MyFinal"(internal) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION my_type_from_text(text) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION int_eq3(integer, integer) RETURNS boolean LANGUAGE sql AS $$ SELECT true $$;
CREATE FUNCTION my_type_in(cstring) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION my_type_out(integer) RETURNS cstring LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION my_fdw_handler() RETURNS fdw_handler LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION my_fdw_validator(text[], oid) RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION plsample_call_handler() RETURNS language_handler LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION add_support(internal) RETURNS internal LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION ghost() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;
CREATE FUNCTION audit_note() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;
CREATE FUNCTION fdw_only_validator(text[], oid) RETURNS void LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FUNCTION quoted_trigger_fn() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;
"""
_WIRING = """\
CREATE TRIGGER t2 AFTER INSERT ON items FOR EACH ROW EXECUTE PROCEDURE audit.log_row();
CREATE EVENT TRIGGER ddl_watch ON ddl_command_end
    EXECUTE FUNCTION process_ddl_event();
CREATE AGGREGATE first(integer) (
    SFUNC = first_sfunc,
    STYPE = internal,
    COMBINEFUNC = first_combine,
    FINALFUNC = first_fin
);
CREATE AGGREGATE quoted(integer) (sfunc = first_sfunc, stype = internal, finalfunc = "MyFinal");
CREATE CAST (text AS my_type) WITH FUNCTION my_type_from_text(text) AS IMPLICIT;
CREATE OPERATOR === (LEFTARG = integer, RIGHTARG = integer, FUNCTION = int_eq3);
CREATE TYPE my_type (INPUT = my_type_in, OUTPUT = my_type_out);
CREATE FOREIGN DATA WRAPPER my_fdw HANDLER my_fdw_handler VALIDATOR my_fdw_validator;
CREATE LANGUAGE plsample HANDLER plsample_call_handler;
CREATE FUNCTION add(integer, integer) RETURNS integer LANGUAGE sql
    SUPPORT add_support AS $$ SELECT $1 + $2 $$;
-- EXECUTE FUNCTION ghost();
/* CREATE TRIGGER t3 BEFORE UPDATE ON items FOR EACH ROW EXECUTE FUNCTION ghost(); */
COMMENT ON TABLE items IS 'CREATE TRIGGER t4 AFTER INSERT ON items EXECUTE FUNCTION ghost()';
CREATE FUNCTION rewire() RETURNS void LANGUAGE plpgsql AS $body$
BEGIN
    EXECUTE 'CREATE TRIGGER t5 AFTER INSERT ON items EXECUTE FUNCTION ghost()';
END;
$body$;
CREATE TABLE settings (handler ghost, input ghost);
/* audited */ CREATE TRIGGER t6 AFTER UPDATE ON items FOR EACH ROW -- EXECUTE FUNCTION ghost()
    WHEN (NEW.note <> 'a;b') EXECUTE FUNCTION audit_note();
CREATE FUNCTION typed(support ghost) RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;
CREATE FOREIGN DATA WRAPPER only_validates NO HANDLER VALIDATOR fdw_only_validator;
CREATE TRIGGER "audit;insert" AFTER INSERT ON items FOR EACH ROW EXECUTE FUNCTION quoted_trigger_fn();
CREATE TRIGGER "EXECUTE FUNCTION ghost(" AFTER UPDATE ON items FOR EACH ROW EXECUTE FUNCTION quoted_trigger_fn();
"""

_Graph = tuple[Path, _StatefulIngestor]


def _index(root: Path, files: dict[str, str]) -> _StatefulIngestor:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run(force=True)
    return store


class _Client:
    # `collect_dead_code` reads through the read-only client protocol.
    def __init__(self, store: _StatefulIngestor) -> None:
        self._store = store

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._store.fetch_all(query, None if params is None else dict(params))


def _int(value: PropertyValue) -> int:
    assert isinstance(value, int), value
    return value


def _sites(store: _StatefulIngestor, target: str) -> list[tuple[str, int, int]]:
    # Every CALLS site into `target`: (caller, line, col), one per site.
    sites = []
    for edge in store.keyed_edges:
        if edge[2] == cs.RelationshipType.CALLS and edge[4] == f"{PROJECT}.{target}":
            props = store.props_for(edge)
            caller = str(edge[1]).removeprefix(f"{PROJECT}.")
            sites.append((caller, _int(props[cs.KEY_LINE]), _int(props[cs.KEY_COL])))
    return sorted(sites)


def _callers(store: _StatefulIngestor, target: str) -> dict[str, tuple[int, int]]:
    return {caller: (line, col) for caller, line, col in _sites(store, target)}


@pytest.fixture(scope="module")
def issue(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("sql3181") / PROJECT, {"schema.sql": _SCHEMA})


@pytest.fixture(scope="module")
def wiring(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(
        tmp_path_factory.mktemp("sql3181w") / PROJECT,
        {"routines.sql": _ROUTINES, "wiring.sql": _WIRING},
    )


def test_the_issue_schema_has_no_dead_code(issue: _StatefulIngestor) -> None:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    assert [
        row["qualified_name"]
        for row in collect_dead_code(_Client(issue), PROJECT, config)
    ] == []


def test_every_sql_routine_is_exported(issue: _StatefulIngestor) -> None:
    assert {
        props[cs.KEY_NAME]: props.get(cs.KEY_IS_EXPORTED)
        for (label, _qn), props in issue.nodes.items()
        if label == cs.NodeLabel.FUNCTION
    } == {
        "api_get_item": True,
        "touch": True,
        "sum_sfunc": True,
    }


@pytest.mark.parametrize(
    ("target", "site"),
    [("schema.touch", (16, 34)), ("schema.sum_sfunc", (22, 42))],
    ids=["trigger", "aggregate-sfunc"],
)
def test_the_ddl_naming_a_routine_calls_it_from_its_line(
    issue: _StatefulIngestor, target: str, site: tuple[int, int]
) -> None:
    assert _callers(issue, target) == {"schema": site}


@pytest.mark.parametrize(
    ("target", "line"),
    [
        ("routines.audit.log_row", 1),
        ("routines.process_ddl_event", 3),
        ("routines.first_sfunc", 5),
        ("routines.first_combine", 7),
        ("routines.first_fin", 8),
        ("routines.MyFinal", 10),
        ("routines.my_type_from_text", 11),
        ("routines.int_eq3", 12),
        ("routines.my_type_in", 13),
        ("routines.my_type_out", 13),
        ("routines.my_fdw_handler", 14),
        ("routines.my_fdw_validator", 14),
        ("routines.plsample_call_handler", 15),
        ("routines.add_support", 17),
        ("routines.audit_note", 28),
        ("routines.fdw_only_validator", 30),
        ("routines.quoted_trigger_fn", 31),
        ("routines.quoted_trigger_fn", 32),
    ],
    ids=[
        "trigger-execute-procedure-qualified",
        "event-trigger",
        "aggregate-sfunc",
        "aggregate-combinefunc",
        "aggregate-finalfunc",
        "aggregate-quoted-lowercase-keyword",
        "cast-with-function",
        "operator-function",
        "type-input",
        "type-output",
        "fdw-handler",
        "fdw-validator",
        "language-handler",
        "function-support",
        "trigger-around-comments-and-a-semicolon-string",
        "fdw-validator-after-no-handler",
        "trigger-with-a-semicolon-in-its-quoted-name",
        "trigger-with-syntax-in-its-quoted-name",
    ],
)
def test_each_ddl_clause_references_its_routine(
    wiring: _StatefulIngestor, target: str, line: int
) -> None:
    sites = _sites(wiring, target)
    assert line in {
        site_line for caller, site_line, _ in sites if caller == "wiring"
    }, sites


def test_a_routine_named_only_in_comments_strings_or_bodies_is_not_called(
    wiring: _StatefulIngestor,
) -> None:
    # Negative: the same clauses inside a `--` or `/* */` comment, a string
    # literal, a dollar-quoted routine body and a quoted trigger name are
    # not DDL the file runs, a table's `handler ghost`/`input ghost` columns
    # and a `support ghost` parameter are not routine slots.
    assert _callers(wiring, "routines.ghost") == {}


def test_an_aggregate_counts_its_state_function_once_per_statement(
    wiring: _StatefulIngestor,
) -> None:
    # Negative: `first_sfunc` is named by two aggregates; neither its
    # STYPE nor any other option adds a site.
    assert [line for _c, line, _col in _sites(wiring, "routines.first_sfunc")] == [
        5,
        10,
    ]


def test_a_routine_added_later_links_the_unchanged_ddl_naming_it(
    tmp_path: Path,
) -> None:
    # The trigger file does not change, and no edge leads into it from the
    # added file, so only its waiter on `touch` sends it back for a re-parse;
    # the incremental graph must match a clean index.
    trigger = (
        "CREATE TRIGGER items_touch BEFORE UPDATE ON items\n"
        "    FOR EACH ROW EXECUTE FUNCTION touch();\n"
    )
    routine = "CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$;\n"

    def ddl_edges(store: _StatefulIngestor) -> set[tuple[str, str, int]]:
        return {
            (str(edge[1]), str(edge[4]), _int(store.props_for(edge)[cs.KEY_LINE]))
            for edge in store.keyed_edges
            if edge[2] == cs.RelationshipType.CALLS
        }

    root = tmp_path / "incremental"
    _materialise(root, {"db/wiring.sql": trigger})
    store = _StatefulIngestor()
    _index_into(store, root, cs.SupportedLanguage.SQL, force=True)
    assert ddl_edges(store) == set()
    _add_after_cache(root, "db/touch.sql", routine)
    _index_into(store, root, cs.SupportedLanguage.SQL, force=False)

    clean_root = tmp_path / "clean"
    _materialise(clean_root, {"db/wiring.sql": trigger, "db/touch.sql": routine})
    clean = _StatefulIngestor()
    _index_into(clean, clean_root, cs.SupportedLanguage.SQL, force=True)
    assert (
        ddl_edges(store)
        == ddl_edges(clean)
        == {("proj.db.wiring", "proj.db.touch.touch", 2)}
    )


@pytest.mark.parametrize(
    "edit",
    [
        {"SFUNC = first_sfunc,": "SFUNC = first_fin,"},
        {
            "SFUNC = first_sfunc,": "SFUNC = first_fin  ,",
            "CREATE TABLE settings": "CREATE TABLE settingz",
        },
        {"SFUNC = first_sfunc,": "SFUNC = first_fin  ,"},
    ],
    ids=["shorter", "same-length", "same-length-inside-a-blanked-statement"],
)
def test_ddl_edited_after_the_parse_is_not_read_against_the_old_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, edit: dict[str, str]
) -> None:
    # Negative: an edit landing between the parse and the call pass (a sync
    # racing the editor) is not the text the tree holds, so its DDL must not
    # be read at the tree's offsets: no `first_fin` site on line 5.
    edited = _WIRING
    for old, new in edit.items():
        edited = edited.replace(old, new)
    process = CallProcessor.process_calls_in_file

    def edit_then_process(
        self: CallProcessor, file_path: Path, *args: Any, **kwargs: Any
    ) -> None:
        if file_path.name == "wiring.sql":
            file_path.write_text(edited, encoding="utf-8")
        process(self, file_path, *args, **kwargs)

    monkeypatch.setattr(CallProcessor, "process_calls_in_file", edit_then_process)
    store = _index(
        tmp_path / PROJECT, {"routines.sql": _ROUTINES, "wiring.sql": _WIRING}
    )
    assert 5 not in {line for _c, line, _col in _sites(store, "routines.first_fin")}


def test_ddl_after_a_leading_blank_line_and_comment_references_its_routine(
    tmp_path: Path,
) -> None:
    # The root starts at the first comment, not at byte 0, so the DDL text
    # is cut by the root's span from the bytes the file was parsed from.
    store = _index(
        tmp_path / PROJECT,
        {"routines.sql": _ROUTINES, "wiring.sql": "\n-- wiring\n" + _WIRING},
    )
    assert 7 in {line for _c, line, _col in _sites(store, "routines.first_sfunc")}


def test_an_evicted_tree_reloaded_from_an_edited_file_reads_the_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A one-entry cache evicts wiring.sql's tree before the call pass, which
    # reloads it from disk after the edit below; its DDL must be read from
    # the bytes of that reload, so the edited SFUNC is the one linked.
    monkeypatch.setattr(settings, "CACHE_MAX_ENTRIES", 1)
    edited = _WIRING.replace("SFUNC = first_sfunc,", "SFUNC = first_fin  ,")
    process = CallProcessor.process_calls_in_file

    def edit_then_process(
        self: CallProcessor, file_path: Path, *args: Any, **kwargs: Any
    ) -> None:
        wiring = file_path.with_name("wiring.sql")
        if file_path.name == "routines.sql":
            wiring.write_text(edited, encoding="utf-8")
        process(self, file_path, *args, **kwargs)

    monkeypatch.setattr(CallProcessor, "process_calls_in_file", edit_then_process)
    store = _index(
        tmp_path / PROJECT, {"routines.sql": _ROUTINES, "wiring.sql": _WIRING}
    )
    assert 5 in {line for _c, line, _col in _sites(store, "routines.first_fin")}


def test_a_blanked_statement_that_opens_the_file_references_its_routine(
    tmp_path: Path,
) -> None:
    # The EVENT TRIGGER's ERROR swallows the function after it, so recovery
    # blanks the trigger and the recovered root starts after it: the scan
    # reads the whole file the tree was parsed from, not the root's span.
    store = _index(
        tmp_path / PROJECT,
        {
            "routines.sql": _ROUTINES,
            "events.sql": (
                "CREATE EVENT TRIGGER watch ON ddl_command_end"
                " EXECUTE FUNCTION process_ddl_event();\n"
                "CREATE FUNCTION later() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;\n"
            ),
        },
    )
    assert _callers(store, "routines.process_ddl_event") == {"events": (1, 63)}
