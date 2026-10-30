"""Where a function's identity anchors: the start of its NAME token, which
can sit on a later line than the definition's own start."""

from __future__ import annotations

from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.function_identity import (
    FunctionResolution,
    name_start_point,
)


def _root(language: cs.SupportedLanguage, source: bytes) -> Node:
    parsers, _ = load_parsers()
    return parsers[language].parse(source).root_node


def test_a_named_function_anchors_at_its_name() -> None:
    (func,) = _root(
        cs.SupportedLanguage.PYTHON, b"def   handler():\n    pass\n"
    ).children
    # Line is 1-based, column 0-based: `handler` starts after `def   `.
    assert name_start_point(func) == (1, 6)


def test_a_multiline_receiver_puts_the_name_on_a_later_line() -> None:
    root = _root(
        cs.SupportedLanguage.GO,
        b"package p\n\nfunc (r *T,\n) Name() {}\n",
    )
    method = root.children[-1]
    assert method.type == cs.TS_GO_METHOD_DECLARATION
    assert method.start_point[0] + 1 == 3
    assert name_start_point(method) == (4, 2)


def test_a_definition_without_a_name_anchors_at_its_start() -> None:
    root = _root(cs.SupportedLanguage.PYTHON, b"x = 1\nf = lambda: 1\n")
    lambda_node = root.children[1].children[0].child_by_field_name(cs.FIELD_RIGHT)
    assert lambda_node is not None
    assert lambda_node.type == cs.TS_PY_LAMBDA
    assert name_start_point(lambda_node) == (2, 4)


def test_a_resolution_is_not_anonymous_unless_said() -> None:
    resolution = FunctionResolution("proj.mod.f", "f", True)
    assert resolution.is_anonymous is False
    assert resolution._replace(is_anonymous=True).is_anonymous is True
