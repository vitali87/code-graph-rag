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


def test_dart_env_flows_to_process_run_argument(tmp_path: Path) -> None:
    # `Process.run('sh', [k])` executes a subprocess: the command literal is
    # the PROCESS resource identity and the tainted argument list reaching it
    # models command injection (issue #1224).
    source = (
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  Process.run('sh', ['-c', k]);\n"
        "}\n"
    )
    assert (_ENV_K, "resource::PROCESS::sh") in _run_flow(tmp_path, source)


def test_dart_env_flows_to_socket_write(tmp_path: Path) -> None:
    # `Socket.connect(host, port)` binds a SOCKET handle keyed by the host;
    # `s.write(k)` sends the ENV-tainted value on it (issue #1224).
    source = (
        "void leak() async {\n"
        "  var k = Platform.environment['K'];\n"
        "  var s = await Socket.connect('example.com', 80);\n"
        "  s.write(k);\n"
        "}\n"
    )
    assert (_ENV_K, "resource::SOCKET::example.com") in _run_flow(tmp_path, source)


def test_dart_env_flows_through_string_interpolation_to_process(tmp_path: Path) -> None:
    # Shell payloads are routinely interpolated: `'echo $k'` embeds the
    # tainted expression inside the literal, both in the `$k` and `${...}`
    # forms (issue #1224 review).
    source = (
        "void leak() {\n"
        "  var k = Platform.environment['K'];\n"
        "  Process.run('sh', ['-c', 'echo $k']);\n"
        "}\n"
    )
    assert (_ENV_K, "resource::PROCESS::sh") in _run_flow(tmp_path, source)


def test_dart_file_read_binding_carries_the_resource_taint(tmp_path: Path) -> None:
    # `var d = f.readAsString()` yields data FROM the file: the binding must
    # carry the FILE resource as origin so a later sink links it (issue #1316).
    source = (
        "void leak() {\n"
        "  var f = File('in.txt');\n"
        "  var d = f.readAsString();\n"
        "  print(d);\n"
        "}\n"
    )
    assert ("resource::FILE::in.txt", _STDOUT) in _run_flow(tmp_path, source)


def test_dart_awaited_read_binding_carries_the_resource_taint(
    tmp_path: Path,
) -> None:
    # The async form routes through the await unwrap before the handle-read
    # path; the binding must carry the same origin.
    source = (
        "void leak() async {\n"
        "  var f = File('in.txt');\n"
        "  var d = await f.readAsBytes();\n"
        "  print(d);\n"
        "}\n"
    )
    assert ("resource::FILE::in.txt", _STDOUT) in _run_flow(tmp_path, source)


def test_dart_listen_callback_parameter_carries_the_socket_taint(
    tmp_path: Path,
) -> None:
    # `s.listen((data) { ... })` delivers data FROM the connected socket into
    # the callback parameter; the body is walked with the parameter seeded by
    # the socket's resource identity (issue #1316).
    source = (
        "void leak() async {\n"
        "  var s = await Socket.connect('example.com', 80);\n"
        "  s.listen((data) { print(data); });\n"
        "}\n"
    )
    assert ("resource::SOCKET::example.com", _STDOUT) in _run_flow(tmp_path, source)


def test_dart_listen_callback_optional_and_typed_parameters_seed(
    tmp_path: Path,
) -> None:
    # Optional-positional (`[data]`) parameters sit under a wrapper node and
    # typed parameters carry the type as their first identifier; both forms
    # must still seed (review on #1317).
    source = (
        "void leak() async {\n"
        "  var s = await Socket.connect('example.com', 80);\n"
        "  s.listen(([data]) { print(data); });\n"
        "}\n"
    )
    assert ("resource::SOCKET::example.com", _STDOUT) in _run_flow(tmp_path, source)
    source = (
        "void leak2() async {\n"
        "  var s = await Socket.connect('example.com', 80);\n"
        "  s.listen((String data) { print(data); });\n"
        "}\n"
    )
    assert ("resource::SOCKET::example.com", _STDOUT) in _run_flow(tmp_path, source)


def test_dart_listen_callback_parameter_shadows_an_outer_handle(
    tmp_path: Path,
) -> None:
    # The parameter rebinds the name inside the lambda: a write through it
    # must not reach the OUTER handle's resource.
    source = (
        "void leak() async {\n"
        "  var k = Platform.environment['K'];\n"
        "  var f = File('out.txt');\n"
        "  var s = await Socket.connect('example.com', 80);\n"
        "  s.listen((f) { f.writeAsString(k); });\n"
        "}\n"
    )
    edges = _run_flow(tmp_path, source)
    assert (_ENV_K, "resource::FILE::out.txt") not in edges
