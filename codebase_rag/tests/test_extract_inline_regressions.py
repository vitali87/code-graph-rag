"""Regression tests shared by extract/inline edits."""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_cut_span_swallows_the_blank_lines_after_a_definition(newline: str) -> None:
    from codebase_rag.editing.move import _cut_span
    from codebase_rag.parser_loader import load_parsers

    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    lines = [
        "def wrapper():",
        "    return other()",
        "",
        "",
        "def other():",
        "    return 2",
        "",
    ]
    source = newline.join(lines).encode(cs.ENCODING_UTF8)
    tree = parsers[cs.SupportedLanguage.PYTHON].parse(source)
    wrapper = tree.root_node.children[0]

    cut = _cut_span(source, wrapper)

    # What survives the cut is what the file keeps. Assert on that rather than
    # on the offset, so the test states the user-visible outcome.
    remainder = source[cut.end :].decode(cs.ENCODING_UTF8)
    assert remainder.startswith("def other():"), (
        f"blank separator survived the cut for {newline!r}: {remainder[:20]!r}"
    )
