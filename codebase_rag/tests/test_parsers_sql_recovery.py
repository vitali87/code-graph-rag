"""`parse_sql` and `lost_sql_functions`: a SQL file is re-parsed statement by
statement only when its own tree loses a `CREATE FUNCTION` that parses on
its own, and the copy it re-parses keeps every line and column."""

from __future__ import annotations

import pytest
from tree_sitter import Node, Parser

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.sql.recovery import lost_sql_functions, parse_sql
from codebase_rag.tests.collaborator_support import first, nodes_of

pytest.importorskip("tree_sitter_sql")

# `$1,$2` opens a dollar quote in tree-sitter-sql that swallows what follows
# (issue #3180's reproduction).
_POSITIONAL = (
    "CREATE FUNCTION add(a integer, b integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT $1 + $2 $$;\n\n"
    "CREATE FUNCTION add3(a integer, b integer, c integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT add(add($1,$2), $3) $$;\n\n"
    "CREATE FUNCTION calc(x integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT add(x, 1) $$;\n\n"
    "CREATE FUNCTION total(a integer, b integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT calc(add($1,$2)) $$;\n\n"
    "CREATE FUNCTION report(x integer) RETURNS integer\n"
    "    LANGUAGE sql AS $$ SELECT total(x, x) $$;\n"
)
_CLEAN = "CREATE FUNCTION one() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;\n"
# OUT/INOUT parameters: a header tree-sitter-sql cannot read even alone.
_UNREADABLE = (
    "CREATE FUNCTION pair(IN a integer, OUT b integer, INOUT c integer)\n"
    "    LANGUAGE sql AS $$ SELECT 1, 2 $$;\n"
)
_AFTER = (
    "CREATE FUNCTION two() RETURNS integer LANGUAGE sql AS $$ SELECT one() $$;\n"
    "CREATE FUNCTION three() RETURNS integer LANGUAGE sql AS $$ SELECT two() $$;\n"
)


@pytest.fixture
def parser() -> Parser:
    parsers, _ = load_parsers()
    return parsers[cs.SupportedLanguage.SQL]


def _function_lines(root: Node) -> list[int]:
    return sorted(n.start_point[0] + 1 for n in nodes_of(root, "create_function"))


def _function_names(root: Node) -> set[str]:
    return {
        (first(function, "object_reference").text or b"").decode()
        for function in nodes_of(root, "create_function")
    }


def test_a_file_that_parses_is_its_own_tree(parser: Parser) -> None:
    source = (_CLEAN + _AFTER).encode()
    tree = parse_sql(parser, source)
    assert not tree.root_node.has_error
    assert tree.root_node.text == source
    assert lost_sql_functions(source, tree.root_node) == 0
    assert _function_names(tree.root_node) == {"one", "two", "three"}


def test_a_positional_parameter_loses_functions_the_recovery_finds(
    parser: Parser,
) -> None:
    source = _POSITIONAL.encode()
    assert lost_sql_functions(source, parser.parse(source).root_node) > 0

    recovered = parse_sql(parser, source).root_node

    assert lost_sql_functions(source, recovered) == 0
    # On the file's own lines: the re-parsed copy kept every byte's place.
    assert _function_lines(recovered) == [1, 4, 7, 10, 13]


def test_a_function_in_a_comment_or_string_is_not_expected(parser: Parser) -> None:
    source = (
        "-- CREATE FUNCTION ghost() RETURNS int LANGUAGE sql AS 'x';\n"
        "SELECT 'CREATE FUNCTION ghost2() RETURNS int';\n" + _CLEAN
    ).encode()
    assert lost_sql_functions(source, parser.parse(source).root_node) == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "parse_sql keeps a CREATE FUNCTION whose header does not parse even on "
        "its own (OUT/INOUT parameters): its standalone parse still holds a "
        "create_function inside the ERROR, so _standalone_function counts it "
        "as defining and nothing blanks it. Its ERROR then swallows the next "
        "statement: `two`, which parses on its own, is lost, and `pair` takes "
        "`two`'s body and its CALLS edge to `one`"
    ),
)
def test_a_function_after_an_unreadable_header_is_recovered(parser: Parser) -> None:
    source = (_CLEAN + _UNREADABLE + _AFTER).encode()
    recovered = parse_sql(parser, source).root_node
    assert {"one", "two", "three"} <= _function_names(recovered)
