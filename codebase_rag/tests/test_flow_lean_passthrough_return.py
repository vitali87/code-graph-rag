"""Pass-through return composition for the lean walk (issue #1363). #1168 gave
Python a callee's return summary keyed by a per-call token, so a tainted
argument entering a parameter that reaches the return carries through to the
caller's use of the result. The lean walk built every part of that -- parameter
seeding, the token on the call-site record, the return-param closure -- but its
call-result Taint omitted the token, so the composition never joined and every
lean language silently lost pass-through taint."""

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
    "c_sharp": "A.cs",
    "go": "m.go",
    "rust": "m.rs",
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


_PASSTHROUGH = {
    "javascript": (
        "function forward(v) { return v; }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  const s = forward(t);\n"
        "  console.log(s);\n"
        "}\n"
    ),
    "java": (
        "class A {\n"
        "  static String forward(String v) { return v; }\n"
        "  static void run() {\n"
        '    String t = System.getenv("SECRET");\n'
        "    String s = forward(t);\n"
        "    System.out.println(s);\n"
        "  }\n"
        "}\n"
    ),
    "c_sharp": (
        "using System;\n"
        "class A {\n"
        "  static string Forward(string v) { return v; }\n"
        "  static void Run() {\n"
        '    var t = Environment.GetEnvironmentVariable("SECRET");\n'
        "    var s = Forward(t);\n"
        "    Console.WriteLine(s);\n"
        "  }\n"
        "}\n"
    ),
    "go": (
        "package main\n\n"
        'import (\n\t"fmt"\n\t"os"\n)\n\n'
        "func forward(v string) string { return v }\n\n"
        "func run() {\n"
        '\tt := os.Getenv("SECRET")\n'
        "\ts := forward(t)\n"
        "\tfmt.Println(s)\n"
        "}\n"
    ),
}


@pytest.mark.parametrize("language", sorted(_PASSTHROUGH))
def test_passthrough_helper_carries_taint_to_the_caller(
    tmp_path: Path, language: str
) -> None:
    assert (_ENV, _STDOUT) in _flows(tmp_path, language, _PASSTHROUGH[language])


def test_a_callee_that_discards_its_argument_stays_clean(tmp_path: Path) -> None:
    # The token must resolve to nothing when the parameter never reaches the
    # return, or every call would launder taint into its result.
    source = (
        'function drop(v) { return "constant"; }\n'
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  const s = drop(t);\n"
        "  console.log(s);\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "javascript", source)


def test_taint_follows_the_returned_parameter_not_any_parameter(
    tmp_path: Path,
) -> None:
    # `pick` returns its SECOND parameter, so a secret handed to the first must
    # not reach the sink: the composition is positional, not per-callee.
    clean = (
        "function pick(a, b) { return b; }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        '  const s = pick(t, "clean");\n'
        "  console.log(s);\n"
        "}\n"
    )
    tainted = (
        "function pick(a, b) { return b; }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        '  const s = pick("clean", t);\n'
        "  console.log(s);\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path / "a", "javascript", clean)
    assert (_ENV, _STDOUT) in _flows(tmp_path / "b", "javascript", tainted)


def test_one_call_site_taint_does_not_leak_into_another(tmp_path: Path) -> None:
    # Both calls share a callee, so a summary keyed by the CALLEE would taint the
    # clean call's result too. The per-call token is what keeps them apart.
    source = (
        "function fwd(v) { return v; }\n"
        "function run() {\n"
        "  const t = process.env.SECRET;\n"
        "  fwd(t);\n"
        '  const s = fwd("clean");\n'
        "  console.log(s);\n"
        "}\n"
    )
    assert (_ENV, _STDOUT) not in _flows(tmp_path, "javascript", source)
