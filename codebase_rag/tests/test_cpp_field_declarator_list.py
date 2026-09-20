"""A field declaration listing several declarators names the first function.

`class A { void (*fp)(int), g(); };` declares a function-pointer field and a
method in one `field_declaration`. The first `function_declarator` (the
pointer's) yields no field name, so the name reader must move on to the next
declarator rather than stop at the first one; nothing in the suite pinned that
order before the reader was split for #1669.
"""

from __future__ import annotations

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp import utils as cpp_utils


@pytest.fixture
def cpp_parser():
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    return parsers[cs.SupportedLanguage.CPP]


def _field_declarations(node: Node) -> list[Node]:
    found: list[Node] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == cs.CppNodeType.FIELD_DECLARATION:
            found.append(current)
        stack.extend(current.children)
    return found


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("class A { void (*fp)(int), g(); };", "g"),
        ("class A { void (*a)(), (*b)(), k(); };", "k"),
        ("class A { void g(); };", "g"),
    ],
)
def test_the_first_declarator_with_a_name_wins(
    cpp_parser, source: str, expected: str
) -> None:
    tree = cpp_parser.parse(source.encode())
    declarations = _field_declarations(tree.root_node)
    assert len(declarations) == 1, [d.type for d in declarations]
    assert cpp_utils.extract_function_name(declarations[0]) == expected
