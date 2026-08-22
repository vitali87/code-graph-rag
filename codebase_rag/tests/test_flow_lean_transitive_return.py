"""Transitive pass-through chaining for the lean walk (issue #1363). #1364 made
a DIRECT `return v` carry its argument's taint in every language. A callee that
returns the result of a FURTHER call (`return other(p)`) still ended the chain,
because only the Python walk recorded the `_return_param_edges` hand-off that
`_resolve_return_params` closes over."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

FLOWS_TO = cs.RelationshipType.FLOWS_TO.value
_CAPTURE_IO = resolve_capture([cs.CaptureGroup.IO.value])
_ENV = "resource::ENV::SECRET"
_STDOUT = "resource::STDOUT::<dynamic>"
_FILENAMES = {
    "javascript": "m.js",
    "java": "A.java",
    "go": "m.go",
    "rust": "m.rs",
    "scala": "M.scala",
    "dart": "m.dart",
}


def _flows(tmp_path: Path, language: str, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"parser not available: {language}")
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / _FILENAMES[language]).write_text(source, encoding="utf-8")
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        capture=_CAPTURE_IO,
    ).run()
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock.ensure_relationship_batch.call_args_list
        if str(c.args[1]) == FLOWS_TO
    }


_CHAIN = {
    "javascript": (
        "function inner(v) { return v; }\n"
        "function outer(p) { return inner(p); }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  const s = outer(t);\n"
        "  console.log(s);\n"
        "}\n"
    ),
    "java": (
        "class A {\n"
        "  static String inner(String v) { return v; }\n"
        "  static String outer(String p) { return inner(p); }\n"
        "  static void run() {\n"
        '    String t = System.getenv("SECRET");\n'
        "    String s = outer(t);\n"
        "    System.out.println(s);\n"
        "  }\n"
        "}\n"
    ),
    "go": (
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func inner(v string) string { return v }\n\n"
        "func outer(p string) string { return inner(p) }\n\n"
        "func run() {\n"
        '\tt := os.Getenv("SECRET")\n'
        "\ts := outer(t)\n"
        "\tfmt.Println(s)\n"
        "}\n"
    ),
}


@pytest.mark.parametrize("language", sorted(_CHAIN))
def test_a_wrapper_of_a_wrapper_carries_taint(tmp_path: Path, language: str) -> None:
    assert (_ENV, _STDOUT) in _flows(tmp_path, language, _CHAIN[language])


def test_a_chain_that_drops_the_argument_stays_clean(tmp_path: Path) -> None:
    # `outer` calls `inner` with a CONSTANT, so its own parameter never reaches
    # the return and the caller's result must stay clean.
    source = (
        "function inner(v) { return v; }\n"
        'function outer(p) { return inner("clean"); }\n'
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  const s = outer(t);\n"
        "  console.log(s);\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "javascript", source)


def test_the_chain_follows_the_returned_slot_not_any_slot(tmp_path: Path) -> None:
    # `inner` returns its SECOND parameter. `outer` forwards its own parameter
    # into the FIRST slot, so the secret must not survive the chain.
    source = (
        "function inner(a, b) { return b; }\n"
        'function outer(p) { return inner(p, "clean"); }\n'
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  const s = outer(t);\n"
        "  console.log(s);\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "javascript", source)


def test_three_deep_chain_still_resolves(tmp_path: Path) -> None:
    # The closure is transitive, so depth must not matter.
    source = (
        "function a(v) { return v; }\n"
        "function b(v) { return a(v); }\n"
        "function c(v) { return b(v); }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  console.log(c(t));\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "javascript", source)


# Keyword-less and selector-based returns reach the hand-off through paths of
# their own, so each needs its own chain fixture (issue #1363 review round 2).
_KEYWORDLESS_CHAIN = {
    "rust": (
        "fn inner(v: String) -> String { v }\n"
        "fn outer(p: String) -> String { inner(p) }\n"
        "fn run() {\n"
        '    let t = std::env::var("SECRET").unwrap();\n'
        '    println!("{}", outer(t));\n'
        "}\n"
    ),
    "scala": (
        "object M {\n"
        "  def inner(v: String): String = v\n"
        "  def outer(p: String): String = inner(p)\n"
        "  def run(): Unit = {\n"
        '    val t = System.getenv("SECRET")\n'
        "    println(outer(t))\n"
        "  }\n"
        "}\n"
    ),
    "dart": (
        "String inner(String v) { return v; }\n"
        "String outer(String p) { return inner(p); }\n"
        "void run() {\n"
        "  var t = Platform.environment['SECRET'];\n"
        "  print(outer(t));\n"
        "}\n"
    ),
}


@pytest.mark.parametrize("language", sorted(_KEYWORDLESS_CHAIN))
def test_chains_resolve_on_the_language_specific_return_paths(
    tmp_path: Path, language: str
) -> None:
    # Rust and Scala return their body's value with no `return` keyword, and
    # Dart resolves calls as a selector chain rather than the shared call node.
    # All three bypass the generic recorder, so each is wired separately.
    assert (_ENV, _STDOUT) in _flows(tmp_path, language, _KEYWORDLESS_CHAIN[language])


def test_a_parenthesized_returned_call_still_chains(tmp_path: Path) -> None:
    # `return (inner(p))` is the same hand-off wearing a wrapper.
    source = (
        "function inner(v) { return v; }\n"
        "function outer(p) { return (inner(p)); }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  console.log(outer(t));\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "javascript", source)


def test_an_awaited_returned_call_still_chains(tmp_path: Path) -> None:
    # `return await inner(p)` likewise: awaiting a value does not change it.
    source = (
        "function inner(v) { return v; }\n"
        "async function outer(p) { return await inner(p); }\n"
        "async function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  console.log(await outer(t));\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) in _flows(tmp_path, "javascript", source)


def test_a_wrapped_chain_that_drops_the_argument_stays_clean(tmp_path: Path) -> None:
    # Unwrapping must not become a blanket "any returned call forwards taint":
    # the wrapped form has to keep the same positional discipline.
    source = (
        "function inner(v) { return v; }\n"
        'function outer(p) { return (inner("clean")); }\n'
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  console.log(outer(t));\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "javascript", source)
