from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag.parser_loader import _create_highlights_query, load_parsers
from codebase_rag.parsers.utils import (
    _decorator_tail_names,
    extract_modifiers_and_decorators,
)
from codebase_rag.tests.test_function_ingest import find_first_node_of_type, parse_code


def test_decorator_tail_names_with_arguments() -> None:
    decorators = ["@cached_property(ttl=3600)", "#[test]", "@abc.abstractmethod()"]
    result = _decorator_tail_names(decorators)
    assert result == {"cached_property", "test", "abstractmethod"}


def test_python_def_class_not_in_modifiers() -> None:
    parsers, queries = load_parsers()

    code = "def my_func(): pass"
    root = parse_code(code, cs.SupportedLanguage.PYTHON, parsers)
    func_node = find_first_node_of_type(root, "function_definition")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.PYTHON]

    modifiers, _ = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "def" not in modifiers

    code_class = "class MyClass: pass"
    root_class = parse_code(code_class, cs.SupportedLanguage.PYTHON, parsers)
    class_node = find_first_node_of_type(root_class, "class_definition")
    assert class_node is not None

    modifiers_class, _ = extract_modifiers_and_decorators(class_node, lang_queries)
    assert "class" not in modifiers_class


def test_python_decorated_definition() -> None:
    parsers, queries = load_parsers()

    code_dec = "@cached_property\ndef decorated_func(): pass"
    root_dec = parse_code(code_dec, cs.SupportedLanguage.PYTHON, parsers)
    func_node_dec = find_first_node_of_type(root_dec, "function_definition")
    assert func_node_dec is not None
    lang_queries = queries[cs.SupportedLanguage.PYTHON]

    _, decorators = extract_modifiers_and_decorators(func_node_dec, lang_queries)
    assert "@cached_property" in decorators


def test_rust_fn_not_in_modifiers() -> None:
    parsers, queries = load_parsers()

    code = "fn my_func() {}"
    root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
    func_node = find_first_node_of_type(root, "function_item")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.RUST]

    modifiers, _ = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "fn" not in modifiers


def test_rust_outer_attributes_captured() -> None:
    parsers, queries = load_parsers()

    code = "#[test]\n#[derive(Debug)]\nfn my_func() {}"
    root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
    func_node = find_first_node_of_type(root, "function_item")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.RUST]

    _, decorators = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "#[test]" in decorators
    assert "#[derive(Debug)]" in decorators


def test_fallback_scm_loads_on_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    parsers, queries = load_parsers()

    real_lang = parsers[cs.SupportedLanguage.PYTHON].language
    assert real_lang is not None

    def mock_import_module(name: str) -> None:
        raise ImportError("Mocked import failure")

    monkeypatch.setattr("importlib.import_module", mock_import_module)

    query = _create_highlights_query(real_lang, cs.SupportedLanguage.PYTHON)
    assert query is not None


def test_fallback_scm_missing_and_import_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    parsers, queries = load_parsers()

    real_lang = parsers[cs.SupportedLanguage.PYTHON].language
    assert real_lang is not None

    def mock_import_module(name: str) -> None:
        raise ImportError("Mocked import failure")

    monkeypatch.setattr("importlib.import_module", mock_import_module)

    from pathlib import Path

    original_exists = Path.exists

    def mock_exists(self: Path) -> bool:
        if "highlights" in str(self) and "python.scm" in str(self):
            return False
        return original_exists(self)

    monkeypatch.setattr(Path, "exists", mock_exists)

    query = _create_highlights_query(real_lang, cs.SupportedLanguage.PYTHON)
    assert query is None


def test_fallback_scm_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    parsers, queries = load_parsers()

    real_lang = parsers[cs.SupportedLanguage.PYTHON].language
    assert real_lang is not None

    def mock_import_module(name: str) -> None:
        raise ImportError("Mocked import failure")

    monkeypatch.setattr("importlib.import_module", mock_import_module)

    from pathlib import Path

    original_read_text = Path.read_text

    def mock_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if "highlights" in str(self) and "python.scm" in str(self):
            raise OSError("Mocked read failure")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mock_read_text)

    query = _create_highlights_query(real_lang, cs.SupportedLanguage.PYTHON)
    assert query is None


def test_typescript_decorated_method() -> None:
    parsers, queries = load_parsers()
    code = "class Foo { @methodDec decoratedMethod(): void {} }"
    root = parse_code(code, cs.SupportedLanguage.TS, parsers)
    method_node = find_first_node_of_type(root, cs.TS_METHOD_DEFINITION)
    assert method_node is not None
    lang_queries = queries[cs.SupportedLanguage.TS]
    _, decorators = extract_modifiers_and_decorators(method_node, lang_queries)
    assert "@methodDec" in decorators


def test_tsx_decorated_method() -> None:
    parsers, queries = load_parsers()
    code = "class Foo { @methodDec decoratedMethod(): void {} }"
    root = parse_code(code, cs.SupportedLanguage.TSX, parsers)
    method_node = find_first_node_of_type(root, cs.TS_METHOD_DEFINITION)
    assert method_node is not None
    lang_queries = queries[cs.SupportedLanguage.TSX]
    assert lang_queries["highlights"] is not None, "TSX highlights query should load"
    _, decorators = extract_modifiers_and_decorators(method_node, lang_queries)
    assert "@methodDec" in decorators


def test_java_modifiers_present() -> None:
    parsers, queries = load_parsers()
    code = "public static void foo() {}"
    root = parse_code(code, cs.SupportedLanguage.JAVA, parsers)
    func_node = find_first_node_of_type(root, "method_declaration")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.JAVA]
    modifiers, _ = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "public" in modifiers
    assert "static" in modifiers


def test_php_single_attribute_captured_once() -> None:
    parsers, queries = load_parsers()
    code = "<?php #[Route('/x')] function foo() {}"
    root = parse_code(code, cs.SupportedLanguage.PHP, parsers)
    func_node = find_first_node_of_type(root, cs.TS_PHP_FUNCTION_DEFINITION)
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.PHP]
    _, decorators = extract_modifiers_and_decorators(func_node, lang_queries)
    assert decorators == ["#[Route('/x')]"]


def test_php_multiple_distinct_attributes() -> None:
    parsers, queries = load_parsers()
    code = "<?php #[Route('/x')] #[Deprecated] function foo() {}"
    root = parse_code(code, cs.SupportedLanguage.PHP, parsers)
    func_node = find_first_node_of_type(root, cs.TS_PHP_FUNCTION_DEFINITION)
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.PHP]
    _, decorators = extract_modifiers_and_decorators(func_node, lang_queries)
    assert decorators == ["#[Route('/x')]", "#[Deprecated]"]


def test_every_language_loads_a_highlights_query() -> None:
    # (H) A highlights query that fails to compile degrades SILENTLY to None
    # (H) (a debug log at startup), zeroing modifiers and decorators for the
    # (H) whole language -- javascript.scm shipped TS-only tokens for months
    # (H) unnoticed (issue #525). Every parsed language must load one.
    parsers, queries = load_parsers()
    missing = [
        str(lang)
        for lang, lq in queries.items()
        if lang in parsers and lq.get(cs.QUERY_HIGHLIGHTS) is None
    ]
    assert missing == [], missing


def test_js_method_modifiers_and_decorators_captured() -> None:
    # (H) The JS grammar has no public/private/protected tokens (those are
    # (H) TS-only); the fallback query must still capture the JS-valid
    # (H) modifiers and decorators.
    parsers, queries = load_parsers()
    code = "class A {\n  @dec\n  static async foo() {}\n}"
    root = parse_code(code, cs.SupportedLanguage.JS, parsers)
    method_node = find_first_node_of_type(root, "method_definition")
    assert method_node is not None
    lang_queries = queries[cs.SupportedLanguage.JS]
    modifiers, decorators = extract_modifiers_and_decorators(method_node, lang_queries)
    assert "static" in modifiers, (modifiers, decorators)
    assert "async" in modifiers, (modifiers, decorators)
    assert "@dec" in decorators, (modifiers, decorators)


def test_dart_annotations_and_modifiers_captured() -> None:
    parsers, queries = load_parsers()
    code = "class A {\n  @override\n  static void foo() {}\n}"
    root = parse_code(code, cs.SupportedLanguage.DART, parsers)
    func_node = find_first_node_of_type(root, "function_signature")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.DART]
    modifiers, decorators = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "static" in modifiers, (modifiers, decorators)
    assert "@override" in decorators, (modifiers, decorators)
