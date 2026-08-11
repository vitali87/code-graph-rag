from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.utils import (
    cpp_positional_parameter_slots,
    go_positional_parameter_slots,
    js_ts_positional_parameter_slots,
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


def test_cpp_slots_unnamed_and_named() -> None:
    assert cpp_positional_parameter_slots(
        _cpp_func("void f(int, const char* msg) {}")
    ) == ([None, "msg"], None)


def test_cpp_slots_no_parameters() -> None:
    assert cpp_positional_parameter_slots(_cpp_func("void f() {}")) == ([], None)
