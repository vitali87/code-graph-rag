"""`ddl_routine_references`: the routines a SQL file's DDL names without an
invocation, read from the source outside comments, strings and bodies, and
only from the slots the statement kind defines."""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.sql.ddl import DdlRoutineReference, ddl_routine_references


def _names(sql: str) -> list[str]:
    return [ref.name for ref in ddl_routine_references(sql.encode())]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "CREATE TRIGGER t BEFORE UPDATE ON items\n"
            "    FOR EACH ROW EXECUTE FUNCTION touch();",
            ["touch"],
        ),
        (
            "CREATE OR REPLACE CONSTRAINT TRIGGER t AFTER INSERT ON x "
            "EXECUTE PROCEDURE audit.log_it();",
            ["audit.log_it"],
        ),
        (
            "CREATE AGGREGATE my_sum(integer) "
            "(SFUNC = sum_sfunc, STYPE = integer, FINALFUNC = sum_final);",
            ["sum_sfunc", "sum_final"],
        ),
        ("CREATE CAST (text AS my_t) WITH FUNCTION to_my_t(text);", ["to_my_t"]),
        (
            "CREATE OPERATOR === (LEFTARG = int, RIGHTARG = int, FUNCTION = eq3);",
            ["eq3"],
        ),
        (
            "CREATE TYPE box3 (INPUT = box3_in, OUTPUT = box3_out);",
            ["box3_in", "box3_out"],
        ),
        ("ALTER FUNCTION f(int) SUPPORT f_support;", ["f_support"]),
    ],
)
def test_each_statement_kind_names_its_routines(sql: str, expected: list[str]) -> None:
    assert _names(sql) == expected


def test_a_slot_keyword_naming_no_routine_is_read_past() -> None:
    sql = "CREATE FOREIGN DATA WRAPPER w NO HANDLER VALIDATOR check_opts;"
    assert _names(sql) == ["check_opts"]


def test_a_clause_inside_parentheses_is_a_parameter_not_a_slot() -> None:
    sql = "CREATE FUNCTION f(support my_type) RETURNS int LANGUAGE sql AS 'x';"
    assert _names(sql) == []


@pytest.mark.parametrize(
    "sql",
    [
        "-- CREATE TRIGGER t BEFORE UPDATE ON x EXECUTE FUNCTION touch();\n",
        "/* CREATE TRIGGER t BEFORE UPDATE ON x EXECUTE FUNCTION touch(); */\n",
        "SELECT 'CREATE TRIGGER t BEFORE UPDATE ON x EXECUTE FUNCTION touch()';\n",
        "CREATE FUNCTION f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN EXECUTE "
        "'x'; END $$; CREATE TABLE t (handler text);\n",
    ],
    ids=["line-comment", "block-comment", "string", "body-and-column"],
)
def test_ddl_inside_a_comment_string_or_body_names_nothing(sql: str) -> None:
    assert _names(sql) == []


def test_a_name_is_normalized_as_the_server_folds_it() -> None:
    # Unquoted folds to lower case; a quoted part keeps its case.
    quoted = 'CREATE TRIGGER t AFTER INSERT ON x EXECUTE FUNCTION "Audit"."Log"();'
    bare = "CREATE TRIGGER t AFTER INSERT ON x EXECUTE FUNCTION Audit.LOG();"
    assert _names(quoted) == ["Audit.Log"]
    assert _names(bare) == ["audit.log"]


def test_the_site_is_the_names_span_in_the_file() -> None:
    sql = b"\nCREATE TRIGGER t\n  AFTER INSERT ON x EXECUTE FUNCTION touch();"
    (ref,) = ddl_routine_references(sql)
    assert ref == DdlRoutineReference(
        "touch",
        {cs.KEY_LINE: 3, cs.KEY_COL: 37, cs.KEY_END_LINE: 3, cs.KEY_END_COL: 42},
    )


def test_a_text_placed_in_its_file_shifts_its_first_lines_column() -> None:
    sql = b"CREATE TRIGGER t AFTER INSERT ON x EXECUTE FUNCTION touch();"
    (ref,) = ddl_routine_references(sql, start_row=4, start_col=2)
    assert (ref.site[cs.KEY_LINE], ref.site[cs.KEY_COL]) == (5, 54)
