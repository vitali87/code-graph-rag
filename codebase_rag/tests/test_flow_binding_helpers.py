# Unit tests for the flow walker's binding-target and Rust format-capture
# helpers, which the end-to-end flow tests reach only on common shapes.
from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access.processor import FlowProcessor


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{a} {b:?} {c}", {"a", "b", "c"}),
        ("{{a}} }}{b}", {"b"}),
        ("{} {0} {1:>4}", set()),
        ("{a", set()),
        ("{{{a}}}", {"a"}),
        ("{9x} {_ok1}", {"_ok1"}),
    ],
    ids=["plain", "escaped", "positional", "unclosed", "nested-escape", "ident"],
)
def test_format_capture_names(template: str, expected: set[str]) -> None:
    assert FlowProcessor._format_capture_names(template) == expected


def _first(root: Node, node_type: str) -> Node:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == node_type:
            return node
        stack.extend(reversed(node.children))
    raise AssertionError(f"no {node_type} node")


def _js_root(source: bytes) -> Node:
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.JS not in parsers:
        pytest.skip("javascript tree-sitter grammar not installed")
    return parsers[cs.SupportedLanguage.JS].parse(source).root_node


def _texts(nodes: list[Node]) -> list[str]:
    return [n.text.decode() for n in nodes if n.text is not None]


def test_a_pair_pattern_binds_its_value_side() -> None:
    processor = FlowProcessor.__new__(FlowProcessor)
    pair = _first(_js_root(b"const { key: local } = obj;\n"), cs.TS_PAIR_PATTERN)
    assert _texts(processor._js_binding_children(pair)) == ["local"]


def test_an_assignment_pattern_binds_its_left_side() -> None:
    processor = FlowProcessor.__new__(FlowProcessor)
    default = _first(
        _js_root(b"function f(a = 1) { return a; }\n"), cs.TS_ASSIGNMENT_PATTERN
    )
    assert _texts(processor._js_binding_children(default)) == ["a"]


def test_destructuring_patterns_bind_every_named_child() -> None:
    processor = FlowProcessor.__new__(FlowProcessor)
    array = _first(_js_root(b"const [x, y] = pair;\n"), cs.TS_ARRAY_PATTERN)
    assert _texts(processor._js_binding_children(array)) == ["x", "y"]


def test_an_unrelated_node_binds_nothing() -> None:
    processor = FlowProcessor.__new__(FlowProcessor)
    number = _first(_js_root(b"const n = 1;\n"), "number")
    assert processor._js_binding_children(number) == []
