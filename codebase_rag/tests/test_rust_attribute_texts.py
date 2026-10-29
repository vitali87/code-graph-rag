# The attributes an inline Rust module carries decide whether it is test-only
# (`#[cfg(test)]`); `attribute_texts` reads them as written.
from __future__ import annotations

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.rust.utils import attribute_texts

tsrust = pytest.importorskip("tree_sitter_rust")


@pytest.fixture
def rust_parser() -> Parser:
    return Parser(Language(tsrust.language()))


def _first(parser: Parser, code: bytes, node_type: str):
    tree = parser.parse(code)
    return next(c for c in tree.root_node.children if c.type == node_type)


def test_one_outer_attribute(rust_parser: Parser) -> None:
    node = _first(rust_parser, b"#[derive(Debug)]\nstruct S {}", cs.TS_RS_STRUCT_ITEM)

    assert attribute_texts(node) == ["#[derive(Debug)]"]


def test_outer_attributes_keep_their_order(rust_parser: Parser) -> None:
    node = _first(
        rust_parser,
        b"#[derive(Debug, Clone)]\n#[allow(dead_code)]\nstruct S {}",
        cs.TS_RS_STRUCT_ITEM,
    )

    assert attribute_texts(node) == ["#[derive(Debug, Clone)]", "#[allow(dead_code)]"]


def test_a_doc_comment_between_attribute_and_item_is_skipped(
    rust_parser: Parser,
) -> None:
    node = _first(rust_parser, b"#[cfg(test)]\n/// docs\nmod m {}", cs.TS_RS_MOD_ITEM)

    assert attribute_texts(node) == ["#[cfg(test)]"]


def test_an_item_without_attributes_has_none(rust_parser: Parser) -> None:
    node = _first(rust_parser, b"fn f() {}", cs.TS_RS_FUNCTION_ITEM)

    assert attribute_texts(node) == []


def test_an_inner_attribute_in_the_body_counts(rust_parser: Parser) -> None:
    node = _first(
        rust_parser, b"mod m {\n    #![allow(dead_code)]\n}", cs.TS_RS_MOD_ITEM
    )

    assert attribute_texts(node) == ["#![allow(dead_code)]"]
