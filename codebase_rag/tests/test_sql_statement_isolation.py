"""One SQL statement the grammar cannot handle costs only that statement.

tree-sitter-sql reads `$1,$` in `f($1,$2)` as a dollar-quote tag, opens
another at the `$llar$` of the legal identifier `do$llar$s`, and does not
always resynchronize at `;` after a plpgsql body it cannot parse. Every
well-formed `CREATE FUNCTION` such a region covered vanished from the
graph, with its calls, and the sync said nothing (issue #3180:
TimescaleDB lost 24 such functions, PostgREST's fixtures 118).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp.preproc_recovery import parse_with_preproc_recovery
from codebase_rag.tests.test_incremental_added_dependents import _add_after_cache
from codebase_rag.tests.test_incremental_deleted_dependents import (
    _index as _index_into,
)
from codebase_rag.tests.test_incremental_deleted_dependents import _materialise
from codebase_rag.types_defs import PropertyValue
from evals.cgr_graph import _StatefulIngestor

pytest.importorskip("tree_sitter_sql")

PROJECT = "sqlpp"

# The issue's reproduction, verbatim.
_REPRO = """\
CREATE FUNCTION add(a integer, b integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT $1 + $2 $$;

CREATE FUNCTION add3(a integer, b integer, c integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT add(add($1,$2), $3) $$;

CREATE FUNCTION calc(x integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT add(x, 1) $$;

CREATE FUNCTION total(a integer, b integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT calc(add($1,$2)) $$;

CREATE FUNCTION report(x integer) RETURNS integer
    LANGUAGE sql AS $$ SELECT total(x, x) $$;
"""
_ONE = "CREATE FUNCTION one() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;\n"
_AFTER = (
    "CREATE FUNCTION two() RETURNS integer LANGUAGE sql AS $$ SELECT one() $$;\n"
    "CREATE FUNCTION three() RETURNS integer LANGUAGE sql AS $$ SELECT two() $$;\n"
)
_NEIGHBOURS = {
    "identifier-dollar": "CREATE TABLE do$llar$s (a$num$ numeric);\n",
    "plpgsql-body": (
        "CREATE FUNCTION loud() RETURNS void LANGUAGE plpgsql AS $$\n"
        "BEGIN\n    RAISE NOTICE 'x %', one();\n    PERFORM one();\nEND;\n$$;\n"
    ),
    "procedure": (
        "CREATE PROCEDURE refresh(job_id integer) LANGUAGE plpgsql AS $$\n"
        "BEGIN\n    PERFORM one();\n    COMMIT;\nEND;\n$$;\n"
    ),
    "nested-dollar-tags": (
        "CREATE FUNCTION dyn(t text) RETURNS void LANGUAGE plpgsql AS $_$\n"
        "BEGIN\n    EXECUTE FORMAT($exec$ SELECT one() FROM %I $exec$, t);\nEND;\n"
        "$_$;\n"
    ),
}
# A positional parameter the recovery would rewrite, were it to run.
_INC = "CREATE FUNCTION inc(a integer) RETURNS integer LANGUAGE sql AS $$ SELECT $1 + 1 $$;\n"
_OUT_PARAMS = (
    "CREATE FUNCTION pair(IN a integer, OUT b integer, INOUT c integer)\n"
    "    LANGUAGE sql AS $$ SELECT one(), 2 $$;\n"
)


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


def _int(value: PropertyValue) -> int:
    assert isinstance(value, int), value
    return value


def _functions(store: _StatefulIngestor) -> dict[str, tuple[int, int]]:
    return {
        str(qn).removeprefix(f"{PROJECT}."): (
            _int(props[cs.KEY_START_LINE]),
            _int(props[cs.KEY_END_LINE]),
        )
        for (label, qn), props in store.nodes.items()
        if label == cs.NodeLabel.FUNCTION
    }


def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (
            str(edge[1]).removeprefix(f"{PROJECT}."),
            str(edge[4]).removeprefix(f"{PROJECT}."),
        )
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS
    }


@pytest.fixture(scope="module")
def repro(tmp_path_factory: pytest.TempPathFactory) -> _StatefulIngestor:
    return _index(tmp_path_factory.mktemp("sql3180") / PROJECT, {"schema.sql": _REPRO})


def test_every_repro_function_is_indexed_on_its_own_lines(
    repro: _StatefulIngestor,
) -> None:
    assert _functions(repro) == {
        "schema.add": (1, 2),
        "schema.add3": (4, 5),
        "schema.calc": (7, 8),
        "schema.total": (10, 11),
        "schema.report": (13, 14),
    }


def test_every_repro_call_is_linked(repro: _StatefulIngestor) -> None:
    # `add3`'s own `add($1,$2)` keeps its calls: a body that parses is not
    # hollowed.
    assert _calls(repro) == {
        ("schema.add3", "schema.add"),
        ("schema.calc", "schema.add"),
        ("schema.total", "schema.calc"),
        ("schema.total", "schema.add"),
        ("schema.report", "schema.total"),
    }


@pytest.mark.parametrize("neighbour", list(_NEIGHBOURS), ids=list(_NEIGHBOURS))
def test_a_function_after_a_statement_the_grammar_cannot_read_is_indexed(
    tmp_path: Path, neighbour: str
) -> None:
    store = _index(
        tmp_path / PROJECT, {"db.sql": _ONE + _NEIGHBOURS[neighbour] + _AFTER}
    )
    functions = _functions(store)
    assert {"db.one", "db.two", "db.three"} <= set(functions), functions
    assert {("db.two", "db.one"), ("db.three", "db.two")} <= _calls(store)


def test_a_function_whose_plpgsql_body_does_not_parse_keeps_its_node(
    tmp_path: Path,
) -> None:
    # Its name and span survive; the unparsed body's calls are lost, as
    # before.
    store = _index(tmp_path / PROJECT, {"db.sql": _ONE + _NEIGHBOURS["plpgsql-body"]})
    assert _functions(store).get("db.loud") == (2, 7)


def test_an_unreadable_header_is_reported_by_file_and_count(tmp_path: Path) -> None:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        _index(tmp_path / PROJECT, {"db.sql": _ONE + _OUT_PARAMS + _AFTER})
    finally:
        logger.remove(sink)
    assert [m.strip() for m in messages if "CREATE FUNCTION" in m] == [
        "db.sql: 1 CREATE FUNCTION statement(s) could not be parsed and are not "
        "in the graph (tree-sitter-sql cannot read their header, e.g. OUT/INOUT "
        "parameters)"
    ]


def test_a_recovered_file_logs_no_lost_function(tmp_path: Path) -> None:
    # Negative: every repro function is recovered, so nothing is reported.
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        _index(tmp_path / PROJECT, {"schema.sql": _REPRO})
    finally:
        logger.remove(sink)
    assert not [m for m in messages if "CREATE FUNCTION" in m]


@pytest.mark.parametrize(
    "source",
    [
        _INC + _AFTER,
        _INC
        + "CREATE AGGREGATE my_sum(integer) (SFUNC = inc, STYPE = integer);\n"
        + _AFTER,
    ],
    ids=["no-error", "error-that-loses-no-function"],
)
def test_a_file_that_loses_no_function_is_parsed_as_written(source: str) -> None:
    # Negative: the re-parse is a last resort. A file whose own tree has
    # every function (even with an ERROR elsewhere) keeps that tree, so
    # every byte, `$1` included, reads as written.
    parsers, _ = load_parsers()
    text = source.encode()
    tree = parse_with_preproc_recovery(
        parsers[cs.SupportedLanguage.SQL], text, cs.SupportedLanguage.SQL
    )
    root = tree.root_node
    assert root.text == text[root.start_byte : root.end_byte]


def test_a_recovered_routine_reaches_a_caller_added_before_it(tmp_path: Path) -> None:
    # Incremental: the caller waits on `calc`, which only the recovered
    # parse of the added file defines; the added file's names must come
    # from the same recovery, or the caller is never revisited.
    caller = "CREATE FUNCTION uses_calc() RETURNS integer LANGUAGE sql AS $$ SELECT calc(1) $$;\n"
    root = tmp_path / "incremental"
    _materialise(root, {"db/caller.sql": caller})
    store = _StatefulIngestor()
    _index_into(store, root, cs.SupportedLanguage.SQL, force=True)
    _add_after_cache(root, "db/schema.sql", _REPRO)
    _index_into(store, root, cs.SupportedLanguage.SQL, force=False)
    edges = {
        (str(edge[1]), str(edge[4]))
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS
    }
    assert ("proj.db.caller.uses_calc", "proj.db.schema.calc") in edges
