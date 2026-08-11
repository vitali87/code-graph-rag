"""Forward parameter-taint composition for the lean walk in Java, C#, Rust, and C
(issue #1195, following #1169's Go/JS/TS/C++). A secret handed to a wrapper whose
parameter reaches a sink must connect the source resource to the sink resource,
and the argument must map to the RIGHT positional parameter. Return composition
stays Python-only (a lean callee's return does not forward taint), so only the
parameter-to-sink direction is asserted here."""

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


def _run_flow(tmp_path: Path, files: dict[str, str]) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    missing = [lang for lang in files if lang not in parsers]
    if missing:
        pytest.skip(f"parser(s) not available: {missing}")
    for lang, content in files.items():
        name = {
            "java": "A.java",
            "c_sharp": "A.cs",
            "rust": "m.rs",
            "c": "m.c",
        }[lang]
        (tmp_path / name).write_text(content, encoding="utf-8")
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


def test_java_param_taint_through_logging_wrapper(tmp_path: Path) -> None:
    files = {
        "java": (
            "class A {\n"
            "  static void logIt(String msg) { System.out.println(msg); }\n"
            '  static void caller() { String s = System.getenv("SECRET"); logIt(s); }\n'
            "}\n"
        )
    }
    assert (_ENV, _STDOUT) in _run_flow(tmp_path, files)


def test_java_second_arg_maps_to_second_parameter(tmp_path: Path) -> None:
    # The tainted value is the SECOND argument: it must reach the sink only when
    # the wrapper logs the second parameter, not the first -- the positional
    # mapping #1169 guarantees.
    files = {
        "java": (
            "class A {\n"
            "  static void logIt(String tag, String msg) { System.out.println(msg); }\n"
            '  static void caller() { String s = System.getenv("SECRET"); logIt("app", s); }\n'
            "}\n"
        )
    }
    assert (_ENV, _STDOUT) in _run_flow(tmp_path, files)


def test_java_taint_does_not_reach_the_other_parameter(tmp_path: Path) -> None:
    files = {
        "java": (
            "class A {\n"
            "  static void logIt(String tag, String msg) { System.out.println(tag); }\n"
            '  static void caller() { String s = System.getenv("SECRET"); logIt("app", s); }\n'
            "}\n"
        )
    }
    assert (_ENV, _STDOUT) not in _run_flow(tmp_path, files)


def test_java_untainted_argument_emits_no_flow(tmp_path: Path) -> None:
    files = {
        "java": (
            "class A {\n"
            "  static void logIt(String msg) { System.out.println(msg); }\n"
            '  static void caller() { String s = System.getenv("SECRET"); logIt("constant"); }\n'
            "}\n"
        )
    }
    assert (_ENV, _STDOUT) not in _run_flow(tmp_path, files)


def test_csharp_param_taint_through_logging_wrapper(tmp_path: Path) -> None:
    files = {
        "c_sharp": (
            "class A {\n"
            "  static void LogIt(string msg) { System.Console.WriteLine(msg); }\n"
            "  static void Caller() {\n"
            '    string s = System.Environment.GetEnvironmentVariable("SECRET");\n'
            "    LogIt(s);\n"
            "  }\n"
            "}\n"
        )
    }
    assert (_ENV, _STDOUT) in _run_flow(tmp_path, files)


def test_rust_param_taint_through_macro_wrapper(tmp_path: Path) -> None:
    # Exercises the Rust `println!` macro sink path, which records a
    # parameter-sink summary only once the parameter slot is seeded (#1195).
    files = {
        "rust": (
            'fn log_it(msg: String) { println!("{}", msg); }\n'
            'fn caller() { let s = std::env::var("SECRET").unwrap(); log_it(s); }\n'
        )
    }
    assert (_ENV, _STDOUT) in _run_flow(tmp_path, files)


def test_c_param_taint_through_logging_wrapper(tmp_path: Path) -> None:
    files = {
        "c": (
            "#include <stdlib.h>\n"
            "#include <stdio.h>\n"
            "void log_it(const char* msg) { puts(msg); }\n"
            'void caller() { const char* s = getenv("SECRET"); log_it(s); }\n'
        )
    }
    assert (_ENV, _STDOUT) in _run_flow(tmp_path, files)
