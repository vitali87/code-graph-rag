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

    def test_schema_qualified_name_reduces_to_the_routine(self) -> None:
        # A caller writes the routine name, so that is what it registers under.
        node = _create_function_node(
            "CREATE FUNCTION app.usp_invoice_list() RETURNS int "
            "AS $$ SELECT 1; $$ LANGUAGE sql;"
        )
        assert node is not None
        assert _sql_get_name(node) == "usp_invoice_list"

    def test_returns_none_without_an_object_reference(self) -> None:
        tree_sitter = pytest.importorskip("tree_sitter")
        tree_sitter_sql = pytest.importorskip("tree_sitter_sql")
        language = tree_sitter.Language(tree_sitter_sql.language())
        tree = tree_sitter.Parser(language).parse(b"SELECT 1;")
        assert _sql_get_name(tree.root_node) is None
