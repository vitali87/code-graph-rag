from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import (
    c_positional_parameter_slots,
    cpp_positional_parameter_slots,
    csharp_positional_parameter_slots,
    go_positional_parameter_slots,
    java_positional_parameter_slots,
    js_ts_positional_parameter_slots,
    rust_positional_parameter_slots,
)


def _first_node(lang: str, code: str, node_type: str) -> Node:
    parsers, _ = load_parsers()
    if lang not in parsers:
        pytest.skip(f"{lang} parser not available")
    tree = parsers[lang].parse(code.encode("utf-8"))
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            return node
        stack.extend(node.children)
    raise AssertionError(f"no {node_type} in {lang} source")


def _go_func(code: str) -> Node:
    return _first_node("go", "package main\n" + code, "function_declaration")


def _js_func(code: str) -> Node:
    return _first_node("javascript", code, "function_declaration")


def _cpp_func(code: str) -> Node:
    return _first_node("cpp", code, "function_definition")


def test_go_slots_named_grouped_and_singleton() -> None:
    assert go_positional_parameter_slots(_go_func("func f(a, b int, c string) {}")) == (
        ["a", "b", "c"],
        None,
    )


def test_go_slots_unnamed_params_are_none() -> None:
    assert go_positional_parameter_slots(_go_func("func f(int, string) {}")) == (
        [None, None],
        None,
    )


def test_go_slots_variadic_records_index() -> None:
    assert go_positional_parameter_slots(_go_func("func f(a int, b ...string) {}")) == (
        ["a", "b"],
        1,
    )


def test_go_slots_no_parameters() -> None:
    assert go_positional_parameter_slots(_go_func("func f() {}")) == ([], None)


def test_js_slots_identifiers() -> None:
    assert js_ts_positional_parameter_slots(_js_func("function f(a, b) {}")) == (
        ["a", "b"],
        None,
    )


def test_js_slots_destructuring_default_and_rest() -> None:
    # Destructured -> None (no positional name); default -> the bound name;
    # rest -> a variadic slot whose index is recorded.
    assert js_ts_positional_parameter_slots(
        _js_func("function f(a, {x, y}, b = 1, ...rest) {}")
    ) == (["a", None, "b", "rest"], 3)


def test_js_slots_single_param_arrow_without_parens() -> None:
    arrow = _first_node("javascript", "const f = x => x;", "arrow_function")
    assert js_ts_positional_parameter_slots(arrow) == (["x"], None)


def test_ts_slots_typed_and_destructured_parameters() -> None:
    fn = _first_node(
        "typescript",
        "function f(a: number, {x}: P, c: string) {}",
        "function_declaration",
    )
    assert js_ts_positional_parameter_slots(fn) == (["a", None, "c"], None)


def test_ts_slots_typed_rest_parameter_is_variadic() -> None:
    fn = _first_node(
        "typescript", "function f(...args: string[]) {}", "function_declaration"
    )
    assert js_ts_positional_parameter_slots(fn) == (["args"], 0)


def test_ts_slots_this_parameter_takes_no_slot() -> None:
    # `this` is a type-only annotation, not a runtime argument: `input` must keep
    # index 0 so a first-argument taint maps to it, not shift onto a phantom slot.
    fn = _first_node(
        "typescript",
        "function f(this: Context, input: string) {}",
        "function_declaration",
    )
    assert js_ts_positional_parameter_slots(fn) == (["input"], None)


def test_cpp_slots_unnamed_and_named() -> None:
    assert cpp_positional_parameter_slots(
        _cpp_func("void f(int, const char* msg) {}")
    ) == ([None, "msg"], None)


def test_cpp_slots_no_parameters() -> None:
    assert cpp_positional_parameter_slots(_cpp_func("void f() {}")) == ([], None)


def _java_method(code: str) -> Node:
    return _first_node("java", code, "method_declaration")


def _csharp_method(code: str) -> Node:
    return _first_node("c_sharp", code, "method_declaration")


def _rust_func(code: str) -> Node:
    return _first_node("rust", code, "function_item")


def _c_func(code: str) -> Node:
    return _first_node("c", code, "function_definition")


def test_java_slots_named_and_varargs() -> None:
    assert java_positional_parameter_slots(
        _java_method("class A{void m(String a, int b, String... xs){}}")
    ) == (["a", "b", "xs"], 2)


def test_java_slots_receiver_takes_no_slot() -> None:
    assert java_positional_parameter_slots(
        _java_method("class A{void m(A this, int b){}}")
    ) == (["b"], None)


def test_java_slots_no_parameters() -> None:
    assert java_positional_parameter_slots(_java_method("class A{void m(){}}")) == (
        [],
        None,
    )


def test_csharp_slots_named_and_params_variadic() -> None:
    # `params string[] xs` is not wrapped in a `parameter`; the name is recovered
    # from the trailing identifier and the slot index is marked variadic.
    assert csharp_positional_parameter_slots(
        _csharp_method("class A{void M(string a, int b, params string[] xs){}}")
    ) == (["a", "b", "xs"], 2)


def test_csharp_slots_modifiers_preserve_names() -> None:
    assert csharp_positional_parameter_slots(
        _csharp_method(
            "static class A{static void M(this string a, ref int b, out int c){}}"
        )
    ) == (["a", "b", "c"], None)


def test_csharp_slots_normal_array_is_not_variadic() -> None:
    assert csharp_positional_parameter_slots(
        _csharp_method("class A{void M(int[] arr, int b){}}")
    ) == (["arr", "b"], None)


def test_rust_slots_identifier_mut_reference_and_wildcard() -> None:
    # `&c` is a reference_pattern (unwrap to c); `_` binds no name -> None slot.
    assert rust_positional_parameter_slots(
        _rust_func("fn m(a: i32, mut b: i32, &c: &i32, _: i32) {}")
    ) == (["a", "b", "c", None], None)


def test_rust_slots_self_takes_no_slot() -> None:
    assert rust_positional_parameter_slots(
        _rust_func("impl A { fn m(&self, a: i32) {} }")
    ) == (["a"], None)


def test_rust_slots_ref_mut_and_tuple_destructure() -> None:
    # `&mut a` unwraps past mutable_specifier to a; `ref b` -> b; a tuple_pattern
    # `(x, y)` binds no single positional name -> None slot.
    assert rust_positional_parameter_slots(
        _rust_func("fn m(&mut a: &mut i32, ref b: i32, (x, y): (i32, i32)) {}")
    ) == (["a", "b", None], None)


def test_c_slots_pointer_and_abstract_declarator() -> None:
    assert c_positional_parameter_slots(_c_func("void m(int a, char *b, int) {}")) == (
        ["a", "b", None],
        None,
    )


def test_c_slots_variadic_records_index() -> None:
    assert c_positional_parameter_slots(_c_func("int m(int a, ...) { return 0; }")) == (
        ["a", None],
        1,
    )
