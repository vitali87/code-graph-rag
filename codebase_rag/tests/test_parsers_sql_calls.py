"""`SqlCallResolution`: the name a routine's body calls are attributed under,
the routine an invocation links to, and the routines a file's DDL runs
without an invocation, read from the file as written."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.language_calls import CallName
from codebase_rag.parsers.sql.calls import SqlCallResolution
from codebase_rag.tests.collaborator_support import first, index, parse
from codebase_rag.tests.conftest import get_relationships
from evals.recorded_graph import RecordingIngestor

pytest.importorskip("tree_sitter_sql")

_SCHEMA = (
    "CREATE TABLE items (id integer, updated_at timestamptz);\n"
    "CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$\n"
    "BEGIN NEW.updated_at := now(); RETURN NEW; END;\n$$;\n"
    "CREATE FUNCTION helper(x integer) RETURNS integer LANGUAGE sql AS $$ SELECT x $$;\n"
    "CREATE FUNCTION api(x integer) RETURNS integer LANGUAGE sql AS $$ SELECT helper(x) $$;\n"
    "CREATE TRIGGER items_touch BEFORE UPDATE ON items\n"
    "    FOR EACH ROW EXECUTE FUNCTION touch();\n"
    "-- CREATE TRIGGER old BEFORE UPDATE ON items EXECUTE FUNCTION helper();\n"
)


@pytest.fixture
def graph(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "sq", mock_ingestor, {"db.sql": _SCHEMA})
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.SQL)
    assert isinstance(strategy, SqlCallResolution)
    return mock_ingestor


def test_an_invocation_in_a_routine_body_calls_the_routine(
    graph: RecordingIngestor,
) -> None:
    calls = {(r.source, r.target) for r in get_relationships(graph, "CALLS")}
    assert ("sq.db.api", "sq.db.helper") in calls


def test_a_trigger_references_the_routine_it_executes_from_the_module(
    graph: RecordingIngestor,
) -> None:
    from_module = [r for r in get_relationships(graph, "CALLS") if r.source == "sq.db"]
    assert [(r.target, r.properties[cs.KEY_LINE]) for r in from_module] == [
        ("sq.db.touch", 8)
    ]
    assert from_module[0].properties["resolution"] == cs.EdgeResolution.EXACT


def test_a_commented_out_trigger_references_nothing(graph: RecordingIngestor) -> None:
    callers_of_helper = {
        r.source
        for r in get_relationships(graph, "CALLS")
        if r.target == "sq.db.helper"
    }
    assert callers_of_helper == {"sq.db.api"}


def test_routines_and_invocations_are_named_by_their_object_reference(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "sq", mock_ingestor, {"db.sql": "SELECT 1;\n"})
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.SQL)
    root = parse(
        cs.SupportedLanguage.SQL,
        "CREATE FUNCTION Api.Get(x integer) RETURNS integer LANGUAGE sql "
        "AS $$ SELECT Helper(x) $$;\n",
    )
    assert strategy.caller_name(first(root, "create_function"), "sq.db") == "api.get"
    assert strategy.call_name(first(root, "invocation")) == CallName(True, "helper")
