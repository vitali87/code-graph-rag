"""`retry_without_trailing_annotations` (issue #2552): an ALL_CAPS
annotation macro after a C++ declarator (`bool Check() const
LOCKS_REQUIRED(mu) {`, `int count_ GUARDED_BY(mu);`) is blanked in place and
the file re-parsed; a well-formed file, and every shape that is not such an
annotation, comes back as it went in."""

from __future__ import annotations

import pytest
from tree_sitter import Tree

from codebase_rag import constants as cs
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp.trailing_annotation import (
    retry_without_trailing_annotations,
)


def _retry(source: str) -> tuple[Tree, Tree, bytes, bytes]:
    parsers, _ = load_parsers()
    parser = parsers[cs.SupportedLanguage.CPP]
    raw = source.encode()
    tree = parser.parse(raw)
    retried, text = retry_without_trailing_annotations(parser, tree, raw)
    return tree, retried, raw, text


def _blanked(source: str, macro: str) -> bytes:
    return source.replace(macro, " " * len(macro)).encode()


@pytest.mark.parametrize(
    ("source", "macro"),
    [
        (
            "class Box {\n"
            "  bool Check() const LOCKS_REQUIRED(mu) { return helper(); }\n"
            "  bool helper() const;\n"
            "};\n",
            "LOCKS_REQUIRED(mu)",
        ),
        ("class Box {\n  int count_ GUARDED_BY(mu);\n};\n", "GUARDED_BY(mu)"),
        ("void f() NOEXCEPT_MACRO;\nint g() { return 1; }\n", "NOEXCEPT_MACRO"),
        (
            "int run(const Box& b) LOCKS_REQUIRED(mu) { return 1; }\n",
            "LOCKS_REQUIRED(mu)",
        ),
        # Cut off before the macro, this reads as a variable initialised with `()`.
        ("void C::f() LOCKS_EXCLUDED(mu) {\n  g();\n}\n", "LOCKS_EXCLUDED(mu)"),
    ],
    ids=["method", "data_member", "prototype", "free_function", "out_of_class"],
)
def test_a_trailing_annotation_is_blanked_and_the_file_reparsed(
    source: str, macro: str
) -> None:
    tree, retried, _, text = _retry(source)
    assert tree.root_node.has_error
    assert text == _blanked(source, macro)
    assert not retried.root_node.has_error


def test_an_annotation_spanning_lines_keeps_every_line_break() -> None:
    source = "bool Check() const\n    LOCKS_REQUIRED(mu,\n                   other) { return 1; }\n"
    _, retried, raw, text = _retry(source)
    assert len(text) == len(raw)
    assert text.count(b"\n") == raw.count(b"\n")
    assert b"LOCKS_REQUIRED" not in text
    assert not retried.root_node.has_error


def test_a_well_formed_file_is_never_rescanned() -> None:
    tree, retried, raw, text = _retry("class Box { bool Check() const; };\n")
    assert (retried, text) == (tree, raw)


@pytest.mark.parametrize(
    "source",
    [
        # Not ALL_CAPS: an unknown word, not an annotation macro.
        "class Box {\n  bool Check() const locks_required(mu) { return 1; }\n};\n",
        # `(int a)` declares a parameter: NAME is the function, not a macro.
        "int ATTR(x) NAME(int a);\n",
        # Two glued macro lines: the "type" is a macro, so is the declarator.
        "Q_DISABLE_COPY(X)\nQ_DECLARE_PRIVATE(X)\n",
    ],
    ids=["lowercase", "parameters", "glued_macros"],
)
def test_a_broken_file_without_a_trailing_annotation_is_left_alone(source: str) -> None:
    tree, retried, raw, text = _retry(source)
    assert tree.root_node.has_error
    assert retried is tree
    assert text is raw
