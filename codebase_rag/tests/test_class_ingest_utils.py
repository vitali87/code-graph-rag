"""`class_ingest.utils`: the C# `override` test, which methods a class body
owns (`skip_method`, under both settings of local-definition capture), and
the method nodes a body yields, sliced from a presorted capture list or
queried afresh."""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.class_ingest.utils import (
    body_method_nodes,
    csharp_has_override_modifier,
    decode_node_stripped,
    find_child_by_type,
    skip_method,
)
from codebase_rag.tests.collaborator_support import first, nodes_of, parse

_PY_CLASS = """\
class A:
    def direct(self):
        def inner():
            pass

    class B:
        def nested(self):
            pass
"""


def _name(node: Node) -> str:
    name = node.child_by_field_name(cs.FIELD_NAME)
    assert name is not None
    return decode_node_stripped(name)


def _python_methods() -> tuple[Node, Node, list[Node]]:
    root = parse(cs.SupportedLanguage.PYTHON, _PY_CLASS)
    outer = nodes_of(root, "class_definition")[0]
    body = outer.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    _, queries = load_parsers()
    methods = body_method_nodes(body, queries[cs.SupportedLanguage.PYTHON], None, None)
    assert methods is not None
    return outer, body, methods


def test_only_the_override_modifier_marks_a_csharp_override() -> None:
    root = parse(
        cs.SupportedLanguage.CSHARP,
        "class C : B { public override void M() {} public new void N() {} void P() {} }",
    )
    marked = {
        _name(m): csharp_has_override_modifier(m)
        for m in nodes_of(root, "method_declaration")
    }
    # `new` hides a base member; it is not an override.
    assert marked == {"M": True, "N": False, "P": False}


def test_find_child_by_type_looks_at_direct_children_only() -> None:
    root = parse(cs.SupportedLanguage.CSHARP, "class C : B { void M() {} }")
    declaration = first(root, "class_declaration")
    base_list = find_child_by_type(declaration, "base_list")
    assert base_list is not None
    assert decode_node_stripped(base_list) == ": B"
    # The method is a grandchild, under the declaration_list.
    assert find_child_by_type(declaration, "method_declaration") is None


def test_a_class_owns_its_direct_methods_only_when_capturing_locals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CAPTURE_FUNCTION_LOCAL_DEFINITIONS", True)
    outer, body, methods = _python_methods()
    skipped = {
        _name(m): skip_method(
            m, outer, body, LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON]
        )
        for m in methods
    }
    # A function local to a method, and a nested class's method, belong to
    # their own scopes, not to `A`.
    assert skipped == {"direct": False, "inner": True, "nested": True}


def test_without_local_capture_only_a_function_body_hides_a_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CAPTURE_FUNCTION_LOCAL_DEFINITIONS", False)
    outer, body, methods = _python_methods()
    skipped = {
        _name(m): skip_method(
            m, outer, body, LANGUAGE_SPECS[cs.SupportedLanguage.PYTHON]
        )
        for m in methods
    }
    assert skipped == {"direct": False, "inner": True, "nested": False}


def test_an_item_in_a_rust_const_initializer_is_not_an_impl_method() -> None:
    root = parse(
        cs.SupportedLanguage.RUST,
        "struct S;\n"
        "impl S {\n"
        "    const X: i32 = { fn helper() -> i32 { 1 } helper() };\n"
        "    fn real(&self) {}\n"
        "}\n",
    )
    impl = first(root, "impl_item")
    body = impl.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    _, queries = load_parsers()
    methods = body_method_nodes(body, queries[cs.SupportedLanguage.RUST], None, None)
    assert methods is not None
    skipped = {
        _name(m): skip_method(m, impl, body, LANGUAGE_SPECS[cs.SupportedLanguage.RUST])
        for m in methods
    }
    assert skipped == {"helper": True, "real": False}


def test_a_presorted_capture_list_is_sliced_to_the_body() -> None:
    root = parse(cs.SupportedLanguage.PYTHON, _PY_CLASS)
    functions = nodes_of(root, "function_definition")
    inner_class = nodes_of(root, "class_definition")[1]
    body = inner_class.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    _, queries = load_parsers()
    sliced = body_method_nodes(
        body,
        queries[cs.SupportedLanguage.PYTHON],
        functions,
        [f.start_byte for f in functions],
    )
    assert sliced is not None
    assert [_name(m) for m in sliced] == ["nested"]


def test_a_language_without_a_function_query_yields_no_method_list() -> None:
    root = parse(cs.SupportedLanguage.PYTHON, _PY_CLASS)
    body = first(root, "class_definition").child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    _, queries = load_parsers()
    no_functions = {**queries[cs.SupportedLanguage.PYTHON], cs.QUERY_FUNCTIONS: None}
    # None, not an empty list: the caller stops rather than scanning nothing.
    assert body_method_nodes(body, no_functions, None, None) is None
