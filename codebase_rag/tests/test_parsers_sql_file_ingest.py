"""`SqlFileIngest`: a SQL file read once, the bytes a recovered tree was
parsed from kept for the call pass, and the functions its parse lost
reported."""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.definition_facts import DefinitionFacts
from codebase_rag.parsers.sql.file_ingest import SqlFileIngest
from codebase_rag.parsers.sql.recovery import parse_sql

pytest.importorskip("tree_sitter_sql")

_ONE = "CREATE FUNCTION one() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;\n"
_OUT_PARAMS = (
    "CREATE FUNCTION pair(IN a integer, OUT b integer, INOUT c integer)\n"
    "    LANGUAGE sql AS $$ SELECT 1, 2 $$;\n"
)
# Issue #3180's reproduction: `$1,$2` opens a dollar quote the grammar runs
# on with, so the file is only read in full once the `$`s are rewritten.
_POSITIONAL = (
    "CREATE FUNCTION add(a integer, b integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT $1 + $2 $$;\n\n"
    "CREATE FUNCTION add3(a integer, b integer, c integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT add(add($1,$2), $3) $$;\n\n"
    "CREATE FUNCTION calc(x integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT add(x, 1) $$;\n\n"
    "CREATE FUNCTION total(a integer, b integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT calc(add($1,$2)) $$;\n"
)


def _settle(tmp_path: Path, source: str) -> tuple[DefinitionFacts, Path, list[str]]:
    facts = DefinitionFacts()
    path = tmp_path / "db.sql"
    path.write_text(source, encoding="utf-8")
    parsers, _ = load_parsers()
    data = source.encode()
    root = parse_sql(parsers[cs.SupportedLanguage.SQL], data).root_node
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        SqlFileIngest(facts).settle_parse(path, "db.sql", root, data)
    finally:
        logger.remove(sink)
    return facts, path, messages


def test_a_file_is_read_when_no_bytes_were_given(tmp_path: Path) -> None:
    path = tmp_path / "db.sql"
    path.write_bytes(_ONE.encode())
    ingest = SqlFileIngest(DefinitionFacts())
    assert ingest.read_source(path, None) == _ONE.encode()
    assert ingest.read_source(path, b"given") == b"given"


def test_a_tree_parsed_as_written_keeps_no_copy(tmp_path: Path) -> None:
    facts, path, messages = _settle(tmp_path, _ONE)
    assert path not in facts.sql_sources
    assert messages == []


def test_a_recovered_tree_keeps_the_file_as_written(tmp_path: Path) -> None:
    # The recovered tree's text is the rewritten copy (`$1` -> `_1`), so the
    # call pass reads the file's own bytes from here instead.
    facts, path, _ = _settle(tmp_path, _POSITIONAL)
    assert facts.sql_sources[path] == _POSITIONAL.encode()


def test_a_reparse_that_needs_no_copy_drops_the_stale_one(tmp_path: Path) -> None:
    facts, path, _ = _settle(tmp_path, _POSITIONAL)
    parsers, _ = load_parsers()
    root = parsers[cs.SupportedLanguage.SQL].parse(_ONE.encode()).root_node
    SqlFileIngest(facts).keep_source(path, root, _ONE.encode())
    assert path not in facts.sql_sources


def test_a_lost_function_is_reported_by_file_and_count(tmp_path: Path) -> None:
    _facts, _path, messages = _settle(tmp_path, _ONE + _OUT_PARAMS + _ONE)
    assert [m.strip() for m in messages] == [
        "db.sql: 1 CREATE FUNCTION statement(s) could not be parsed and are not "
        "in the graph (tree-sitter-sql cannot read their header, e.g. OUT/INOUT "
        "parameters)"
    ]


def test_a_parse_from_disk_settles_nothing(tmp_path: Path) -> None:
    facts = DefinitionFacts()
    parsers, _ = load_parsers()
    root = parsers[cs.SupportedLanguage.SQL].parse(_POSITIONAL.encode()).root_node
    SqlFileIngest(facts).settle_parse(tmp_path / "db.sql", "db.sql", root, None)
    assert facts.sql_sources == {}
