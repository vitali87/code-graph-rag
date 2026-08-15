# The Dart collector must capture dispatch through function values and
# map registries (invisible to static analysis) via VM Service CPU samples
# and write a valid interchange trace (issue #1255). Runs the real Dart
# toolchain; skipped when `dart` is not on PATH.

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file

_COLLECTOR_DIR = Path(__file__).resolve().parents[1] / "trace" / "dart_collector"

dart = shutil.which("dart")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(dart is None, reason="dart toolchain not available"),
]

_SAMPLE = """
    final Map<String, String Function()> handlers = {};

    void register(String name, String Function() fn) {
      handlers[name] = fn;
    }

    String handle(String name) {
      return handlers[name]!();
    }

    String greet() {
      var acc = 0;
      for (var i = 0; i < 8000000; i++) {
        acc += i % 3;
      }
      return 'hi$acc';
    }

    void main() {
      register('greet', greet);
      var out = 0;
      for (var round = 0; round < 12; round++) {
        out += handle('greet').length;
      }
      print('total: $out');
    }
"""


_DYNAMIC_SAMPLE = """
    abstract class Animal {
      int speak();
    }

    class Dog implements Animal {
      int speak() {
        var a = 0;
        for (var i = 0; i < 6000000; i++) {
          a += i % 7;
        }
        return a;
      }
    }

    class Cat implements Animal {
      int speak() {
        var a = 0;
        for (var i = 0; i < 6000000; i++) {
          a += i % 5;
        }
        return a;
      }
    }

    int dispatchDynamic(dynamic a) => a.speak();

    void main() {
      var total = 0;
      for (var i = 0; i < 10; i++) {
        dynamic animal = (i % 2 == 0) ? Dog() : Cat();
        total += dispatchDynamic(animal);
      }
      print('total: $total');
    }
"""


def _run_collector(tmp_path: Path, source: str) -> tuple:
    (tmp_path / "main.dart").write_text(textwrap.dedent(source))
    output = tmp_path / "cgr-trace.jsonl"
    subprocess.run(
        [str(dart), "pub", "get"],
        cwd=_COLLECTOR_DIR,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        [
            str(dart),
            "bin/cgr_trace_collect.dart",
            "--repo",
            str(tmp_path),
            "--output",
            str(output),
            "--workload",
            "dart-run",
            "--",
            str(tmp_path / "main.dart"),
        ],
        cwd=_COLLECTOR_DIR,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    header, records = read_trace_file(output)
    return header, list(records)


@pytest.fixture(scope="module")
def dart_trace(tmp_path_factory):
    return _run_collector(tmp_path_factory.mktemp("dyndart"), _SAMPLE)


@pytest.fixture(scope="module")
def dynamic_dispatch_trace(tmp_path_factory):
    return _run_collector(tmp_path_factory.mktemp("dyndispatch"), _DYNAMIC_SAMPLE)


def test_collector_captures_registry_dispatch(dart_trace):
    header, records = dart_trace

    assert header.language == cs.TRACE_LANGUAGE_DART
    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert ("handle", "greet") in edges, sorted(edges)


def test_collector_captures_dynamic_dispatch(dynamic_dispatch_trace):
    # `dispatchDynamic(dynamic a) => a.speak()` calls through a `dynamic`, so
    # the receiver's concrete class is unknowable statically; the VM samples
    # resolve the call to the concrete implementation(s) that actually ran.
    _header, records = dynamic_dispatch_trace

    dispatched = [
        r
        for r in records
        if (r.caller.qualname, r.callee.qualname) == ("dispatchDynamic", "speak")
    ]
    assert dispatched, sorted({(r.caller.qualname, r.callee.qualname) for r in records})
    for record in dispatched:
        assert record.callee.path.endswith("main.dart")
    # Both Dog.speak and Cat.speak run through the one dynamic call site, and
    # each must resolve to its own definition line: a regression that dropped a
    # receiver or collapsed both onto one line would fail here.
    speak_defs = {
        number
        for number, line in enumerate(
            textwrap.dedent(_DYNAMIC_SAMPLE).splitlines(), start=1
        )
        if "int speak() {" in line
    }
    assert len(speak_defs) == 2
    observed = {record.callee.line for record in dispatched}
    assert speak_defs <= observed, (sorted(speak_defs), sorted(observed))


def test_collector_scopes_and_labels_workloads(dart_trace):
    _header, records = dart_trace

    assert records
    for record in records:
        assert record.caller.path.endswith(".dart")
        assert record.callee.path.endswith(".dart")
        assert record.workloads == ("dart-run",)
        assert record.caller.line > 0
        assert record.callee.line > 0
