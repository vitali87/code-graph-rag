"""Branches the #1669 Sonar cleanup touched, pinned so a later edit keeps them.

`_http_reachable` now catches only `OSError`, which must still cover every
failure the opener raises (`URLError` and `TimeoutError` both derive from
it). The Go callee walk now unwraps a parenthesised callee with
`next(iter(...))`, which must still reach the inner name.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.go.type_inference import GoTypeInferenceEngine
from codebase_rag.stack import health
from codebase_rag.stack.health import _http_reachable


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError("refused"),
        TimeoutError("slow"),
        OSError("no route to host"),
        ConnectionResetError("reset"),
    ],
    ids=["url-error", "timeout", "os-error", "connection-reset"],
)
def test_an_unreachable_endpoint_reads_as_down(error: OSError) -> None:
    with patch.object(health._DIRECT_OPENER, "open", side_effect=error):
        assert _http_reachable("http://127.0.0.1:1/health") is False


def _go_call(source: str) -> Node:
    parsers, _ = load_parsers()
    if cs.SupportedLanguage.GO not in parsers:
        pytest.skip("go parser not available")
    stack = [parsers[cs.SupportedLanguage.GO].parse(source.encode()).root_node]
    while stack:
        node = stack.pop()
        if node.type == "call_expression":
            return node
        stack.extend(node.children)
    raise AssertionError("no call_expression")


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("package p\nfunc f() { (g)() }\n", "g"),
        ("package p\nfunc f() { ((g))() }\n", "g"),
        ("package p\nfunc f() { (x.M)() }\n", "M"),
    ],
    ids=["paren", "double-paren", "paren-selector"],
)
def test_a_parenthesised_callee_resolves_to_its_inner_name(
    source: str, name: str
) -> None:
    engine = object.__new__(GoTypeInferenceEngine)
    node = engine._callee_name_node(_go_call(source))

    assert node is not None
    assert node.text == name.encode()
