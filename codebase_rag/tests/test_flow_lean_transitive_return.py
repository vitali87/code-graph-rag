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
_FILENAMES = {"javascript": "m.js", "java": "A.java", "go": "m.go"}


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
