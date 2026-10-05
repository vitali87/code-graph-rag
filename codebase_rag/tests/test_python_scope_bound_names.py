from __future__ import annotations

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.utils import _python_scope_bound_names

SOURCE = b"""
def outer(p):
    def inner(a, b=1):
        x = 1
        for i, j in pairs:
            pass
        with open(p) as fh:
            pass
        import os
        from sys import path as sp
        global g
        match a:
            case [first, _]:
                pass
        @decorator
        def helper():
            hidden = 2
        class Local:
            attr = 3
        return x
    return inner
"""


def _inner_function() -> Node:
    tree = Parser(Language(tsp.language())).parse(SOURCE)
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        name = node.child_by_field_name(cs.FIELD_NAME)
        if (
            node.type == cs.TS_PY_FUNCTION_DEFINITION
            and name is not None
            and name.text == b"inner"
        ):
            return node
        stack.extend(node.children)
    raise AssertionError("inner not found")


def test_every_binding_form_is_collected() -> None:
    bound = _python_scope_bound_names(_inner_function())
    assert {
        "a",
        "b",
        "x",
        "i",
        "j",
        "fh",
        "os",
        "sp",
        "g",
        "first",
        "helper",
        "Local",
    } <= bound


def test_nested_scope_bodies_are_not_descended() -> None:
    bound = _python_scope_bound_names(_inner_function())
    assert "hidden" not in bound
    assert "attr" not in bound
