"""SQL (PostgreSQL) as an indexed language."""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.language_spec import (
    LANGUAGE_FQN_SPECS,
    LANGUAGE_SPECS,
    _sql_get_name,
)


def _create_function_node(source: str):
    """The first create_function node in a SQL snippet."""
    tree_sitter = pytest.importorskip("tree_sitter")
    tree_sitter_sql = pytest.importorskip("tree_sitter_sql")

    language = tree_sitter.Language(tree_sitter_sql.language())
    tree = tree_sitter.Parser(language).parse(source.encode())

    def walk(node):
        if node.type == cs.TS_SQL_CREATE_FUNCTION:
            yield node
        for child in node.children:
            yield from walk(child)

    return next(walk(tree.root_node), None)


class TestSqlLanguageRegistration:
    def test_sql_has_a_language_spec(self) -> None:
        assert cs.SupportedLanguage.SQL in LANGUAGE_SPECS

    def test_sql_has_an_fqn_spec(self) -> None:
        # Without this the definition pass cannot name a routine, and .sql
        # files are walked but never produce callable nodes.
        assert cs.SupportedLanguage.SQL in LANGUAGE_FQN_SPECS

    def test_sql_claims_the_sql_extension(self) -> None:
        spec = LANGUAGE_SPECS[cs.SupportedLanguage.SQL]
        assert ".sql" in spec.file_extensions

    def test_create_function_is_a_function_node(self) -> None:
        spec = LANGUAGE_SPECS[cs.SupportedLanguage.SQL]
        assert cs.TS_SQL_CREATE_FUNCTION in spec.function_node_types

    def test_declared_node_types_exist_in_the_grammar(self) -> None:
        # Naming a node type the grammar does not define fails the language at
        # load time and silently drops SQL entirely, so every declared type
        # must be one the installed grammar knows.
        tree_sitter = pytest.importorskip("tree_sitter")
        tree_sitter_sql = pytest.importorskip("tree_sitter_sql")
        language = tree_sitter.Language(tree_sitter_sql.language())
        spec = LANGUAGE_SPECS[cs.SupportedLanguage.SQL]
        for node_type in (
            *spec.function_node_types,
            *spec.module_node_types,
            *spec.call_node_types,
        ):
            tree_sitter.Query(language, f"({node_type}) @capture")


class TestSqlGetName:
    def test_reads_the_routine_name(self) -> None:
        # create_function names its routine through an object_reference child
        # rather than a `name` field, so the generic extractor finds nothing.
        node = _create_function_node(
            "CREATE FUNCTION usp_invoice_list() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "usp_invoice_list"

    def test_reads_the_name_of_a_replaced_routine(self) -> None:
        node = _create_function_node(
            "CREATE OR REPLACE FUNCTION usp_invoice_list() RETURNS int "
            "AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "usp_invoice_list"

    def test_schema_qualified_name_keeps_its_schema(self) -> None:
        # app.usp_x and audit.usp_x are different routines; collapsing both to
        # usp_x would register them under one key and fan a qualified call
        # onto every schema. The registry indexes the last segment, so an
        # unqualified caller still finds this definition.
        node = _create_function_node(
            "CREATE FUNCTION app.usp_invoice_list() RETURNS int "
            "AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "app.usp_invoice_list"

    def test_same_named_routines_in_different_schemas_stay_distinct(self) -> None:
        names = set()
        for schema in ("app", "audit"):
            node = _create_function_node(
                f"CREATE FUNCTION {schema}.usp_invoice_list() RETURNS int "
                "AS $$ SELECT 1; $$ LANGUAGE sql;"
            )
            assert node is not None
            names.add(_sql_get_name(node))
        assert names == {"app.usp_invoice_list", "audit.usp_invoice_list"}

    def test_quoted_identifiers_lose_their_quotes(self) -> None:
        node = _create_function_node(
            'CREATE FUNCTION "app"."usp_invoice_list"() RETURNS int '
            "AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "app.usp_invoice_list"

    def test_unquoted_identifier_folds_to_lowercase(self) -> None:
        # PostgreSQL folds MyFunc to myfunc; only quoting preserves case.
        node = _create_function_node(
            "CREATE FUNCTION MyFunc() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "myfunc"

    def test_quoted_identifier_keeps_its_case(self) -> None:
        # "MyFunc" and MyFunc are DIFFERENT routines (MyFunc vs myfunc).
        node = _create_function_node(
            'CREATE FUNCTION "MyFunc"() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;'
        )
        assert node is not None
        assert _sql_get_name(node) == "MyFunc"

    def test_quoted_identifier_keeps_a_dot_inside_it(self) -> None:
        # A dot inside quotes is part of the identifier, not a qualifier
        # separator: "billing.v1" is one schema name.
        node = _create_function_node(
            'CREATE FUNCTION "billing.v1".usp_total() RETURNS int '
            "AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "billing.v1.usp_total"

    def test_doubled_quotes_unescape_to_one(self) -> None:
        # Straight through the normalizer: the published grammar cannot yet
        # tokenize a doubled quote inside a quoted identifier (it yields an
        # ERROR node), but the string-call side feeds the normalizer directly
        # and must agree with PostgreSQL.
        from codebase_rag.sql_names import normalize_sql_reference

        assert normalize_sql_reference('"a""b"') == 'a"b'
        assert normalize_sql_reference('app."a""b"') == 'app.a"b'

    def test_returns_none_without_an_object_reference(self) -> None:
        tree_sitter = pytest.importorskip("tree_sitter")
        tree_sitter_sql = pytest.importorskip("tree_sitter_sql")
        language = tree_sitter.Language(tree_sitter_sql.language())
        tree = tree_sitter.Parser(language).parse(b"SELECT 1;")
        assert _sql_get_name(tree.root_node) is None
