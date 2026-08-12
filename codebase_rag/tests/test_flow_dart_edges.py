"""FLOWS_TO lean-walk coverage for Dart (issue #1173). Dart was absent from
`IO_SINKS`, so its modules carried `flow_covered: false` and flow queries over Dart
reported UNKNOWN. Dart has no call-expression node -- calls, member reads and
bindings are flat sibling `selector` chains -- so the walk takes a Dart-specific
leaf path reusing the dart/utils.py reconstructors. These tests exercise a
`Platform.environment` source reaching a `print`/`File` handle sink, tainted
arguments crossing into callees, the return-taint fixpoint, and a branch merge."""

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
_STDOUT = "resource::STDOUT::<dynamic>"
_ENV_K = "resource::ENV::K"


def _run_flow(tmp_path: Path, source: str) -> set[tuple[str, str]]:
    parsers, queries = load_parsers()
    if "dart" not in parsers:
        pytest.skip("dart parser not available")
    (tmp_path / "a.dart").write_text(source, encoding="utf-8")
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


def test_dart_platform_environment_flows_to_print(tmp_path: Path) -> None:
    # `Platform.environment['K']` is a process-env source; `print` writes STDOUT.
    source = "void leak() {\n  var k = Platform.environment['K'];\n  print(k);\n}\n"
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_env_flows_to_file_handle_write(tmp_path: Path) -> None:
    # `File(path)` binds a FILE handle; `f.writeAsString(k)` writes the ENV-tainted
    # value to the file.
    source = (
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  var f = File('out.txt');\n"
        "  f.writeAsString(k);\n"
        "}\n"
    )
    assert (_ENV_K, "resource::FILE::out.txt") in _run_flow(tmp_path, source)


def test_dart_stdout_write_sink(tmp_path: Path) -> None:
    # `stdout.write(x)` is a STDOUT sink reached via the selector-chain call name.
    source = (
        "void leak() {\n  var k = Platform.environment['K'];\n  stdout.write(k);\n}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_tainted_argument_into_callee(tmp_path: Path) -> None:
    # A tainted value passed as an argument reaches a sink inside the callee: the
    # parameter-to-sink summary composes across function bodies.
    source = (
        "void sink(String? m) { print(m); }\n"
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  sink(k);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_return_taint_fixpoint(tmp_path: Path) -> None:
    # A callee returning a tainted value taints the caller's binding through the
    # return-taint fixpoint.
    source = (
        "String? src() { return Platform.environment['K']; }\n"
        "void leak() {\n"
        "  var v = src();\n"
        "  print(v);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_branch_merge_preserves_taint(tmp_path: Path) -> None:
    # Taint assigned in one if arm survives the MAY join to a post-merge sink.
    source = (
        "void leak(bool x) {\n"
        "  var s = '';\n"
        "  if (x) { s = Platform.environment['K']; } else { s = 'safe'; }\n"
        "  print(s);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_expression_bodied_callee(tmp_path: Path) -> None:
    # An arrow-bodied `=> print(v)` sink is walked (Greptile/CodeRabbit review).
    source = (
        "void sink(String? v) => print(v);\n"
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  sink(k);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_expression_bodied_source_return(tmp_path: Path) -> None:
    # An arrow body `=> expr` is the implicit return value, feeding the fixpoint.
    source = (
        "String? src() => Platform.environment['K'];\n"
        "void leak() {\n"
        "  var v = src();\n"
        "  print(v);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_inline_source_argument(tmp_path: Path) -> None:
    # A non-identifier argument is a full chain: `print(Platform.environment['K'])`
    # evaluates the inline source (Greptile/CodeRabbit review).
    source = "void leak() {\n  print(Platform.environment['K']);\n}\n"
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_named_argument_flows_to_sink(tmp_path: Path) -> None:
    # A named argument `sink(message: k)` propagates via `kw:message` to the named
    # parameter (which is flattened out of `optional_formal_parameters` and seeded).
    source = (
        "void sink({String? message}) { print(message); }\n"
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  sink(message: k);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_optional_positional_parameter(tmp_path: Path) -> None:
    # An optional-positional parameter `[String? m]` is flattened into slot 0, so a
    # positional argument still composes to the sink.
    source = (
        "void sink([String? m]) { print(m); }\n"
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  sink(k);\n"
        "}\n"
    )
    assert (_ENV_K, _STDOUT) in _run_flow(tmp_path, source)


def test_dart_untainted_io_emits_no_flow(tmp_path: Path) -> None:
    source = (
        "void leak() {\n"
        "  var f = File('out.txt');\n"
        "  f.writeAsString('literal');\n"
        "  print('constant');\n"
        "}\n"
    )
    assert _run_flow(tmp_path, source) == set()
