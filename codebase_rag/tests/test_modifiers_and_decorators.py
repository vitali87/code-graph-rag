import pytest
from tree_sitter import Node, Parser

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import (
    _decorator_tail_names,
    extract_modifiers_and_decorators,
)


def find_first_node_of_type(root: Node, node_type: str) -> Node | None:
    if root.type == node_type:
        return root
    for child in root.children:
        if result := find_first_node_of_type(child, node_type):
            return result
    return None


def parse_code(
    code: str,
    language: cs.SupportedLanguage,
    parsers: dict[cs.SupportedLanguage, Parser],
) -> Node:
    parser = parsers[language]
    tree = parser.parse(code.encode(cs.ENCODING_UTF8))
    return tree.root_node


def test_decorator_tail_names_with_arguments() -> None:
    decorators = ["@cached_property(ttl=3600)", "#[test]", "@abc.abstractmethod()"]
    result = _decorator_tail_names(decorators)
    assert result == {"cached_property", "test", "abstractmethod"}


def test_python_def_class_not_in_modifiers() -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("Python parser not available")

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


def test_rust_fn_not_in_modifiers() -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("Rust parser not available")

    code = "fn my_func() {}"
    root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
    func_node = find_first_node_of_type(root, "function_item")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.RUST]

    modifiers, _ = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "fn" not in modifiers


def test_rust_outer_attributes_captured() -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.RUST not in parsers:
        pytest.skip("Rust parser not available")

    code = "#[test]\n#[derive(Debug)]\nfn my_func() {}"
    root = parse_code(code, cs.SupportedLanguage.RUST, parsers)
    func_node = find_first_node_of_type(root, "function_item")
    assert func_node is not None
    lang_queries = queries[cs.SupportedLanguage.RUST]

    _, decorators = extract_modifiers_and_decorators(func_node, lang_queries)
    assert "#[test]" in decorators
    assert "#[derive(Debug)]" in decorators


def test_fallback_scm_loads_on_import_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from codebase_rag.parser_loader import _create_highlights_query, load_parsers

    parsers, _ = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("Python parser not available")

    real_lang = parsers[cs.SupportedLanguage.PYTHON].language

    def mock_import_module(name: str) -> None:
        raise ImportError("Mocked import failure")

    monkeypatch.setattr("importlib.import_module", mock_import_module)

    query = _create_highlights_query(real_lang, cs.SupportedLanguage.PYTHON)
    assert query is not None
