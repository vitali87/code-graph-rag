# The sys.monitoring tracer must observe Python-to-Python calls scoped to a
# repository root, including edges static analysis cannot see (dispatch through
# a registry dict) and the concrete receiver type behind an inherited method
# call (issue #1246).

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.tracer import CallGraphTracer


def _start_or_skip(tracer: CallGraphTracer) -> None:
    # When this suite itself runs under `pytest --cgr-trace`, the session's
    # plugin already holds sys.monitoring.PROFILER_ID and a second claim
    # raises; these tests cannot share the slot, so they skip.
    try:
        tracer.start()
    except ValueError:
        pytest.skip("sys.monitoring PROFILER_ID already claimed in this session")


_PKG_FILES = {
    "animals.py": """
        class Animal:
            def speak(self):
                return self._sound()

            def _sound(self):
                return "generic"


        class Dog(Animal):
            def _sound(self):
                return "woof"
    """,
    "registry.py": """
        HANDLERS = {}


        def register(name, fn):
            HANDLERS[name] = fn


        def handle(name):
            return HANDLERS[name]()


        def greet():
            return "hi"


        register("greet", greet)
    """,
    "entry.py": """
        from .animals import Dog
        from .registry import handle


        def run_all():
            dog = Dog()
            dog.speak()
            return handle("greet")
    """,
}


def _write_package(root: Path, package: str) -> None:
    pkg_dir = root / package
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("")
    for name, source in _PKG_FILES.items():
        (pkg_dir / name).write_text(textwrap.dedent(source))


def _trace_package(root: Path, package: str, workload: str | None) -> CallGraphTracer:
    sys.path.insert(0, str(root))
    try:
        entry = importlib.import_module(f"{package}.entry")
        tracer = CallGraphTracer(root)
        _start_or_skip(tracer)
        try:
            if workload is not None:
                tracer.set_workload(workload)
            entry.run_all()
        finally:
            tracer.stop()
        return tracer
    finally:
        sys.path.remove(str(root))
        for module_name in [m for m in sys.modules if m.startswith(package)]:
            del sys.modules[module_name]


def test_tracer_records_registry_dispatch_and_receiver_types(tmp_path):
    package = "dyn_trace_pkg_a"
    _write_package(tmp_path, package)
    tracer = _trace_package(tmp_path, package, workload=None)

    pairs = {(r.caller.qualname, r.callee.qualname): r for r in tracer.records()}
    assert ("run_all", "Animal.speak") in pairs, pairs.keys()
    assert ("Animal.speak", "Dog._sound") in pairs, pairs.keys()
    # The registry dispatch is invisible to static analysis; the tracer must
    # see handle() invoking greet() through the HANDLERS dict.
    assert ("handle", "greet") in pairs, pairs.keys()

    speak = pairs[("run_all", "Animal.speak")]
    assert any(receiver.endswith("animals.Dog") for receiver in speak.receiver_types), (
        speak.receiver_types
    )


def test_tracer_scopes_to_repo_root_and_tags_workloads(tmp_path):
    package = "dyn_trace_pkg_b"
    _write_package(tmp_path, package)
    tracer = _trace_package(tmp_path, package, workload="tests/test_x.py::test_y")

    records = tracer.records()
    assert records
    root = str(tmp_path)
    for record in records:
        assert record.caller.path.startswith(root)
        assert record.callee.path.startswith(root)
        assert record.workloads == ("tests/test_x.py::test_y",)


def test_tracer_excludes_dependency_dirs_under_repo_root(tmp_path):
    tracer = CallGraphTracer(tmp_path)

    assert tracer._in_scope(str(tmp_path / "pkg" / "mod.py"))
    assert not tracer._in_scope(str(tmp_path / ".venv" / "lib" / "dep.py"))
    assert not tracer._in_scope(
        str(tmp_path / ".venv" / "lib" / "site-packages" / "dep.py")
    )
    assert not tracer._in_scope(str(tmp_path / "web" / "node_modules" / "x.py"))


def test_start_releases_tool_id_when_setup_fails(tmp_path, monkeypatch):
    try:
        sys.monitoring.use_tool_id(sys.monitoring.PROFILER_ID, "probe")
    except ValueError:
        pytest.skip("sys.monitoring PROFILER_ID already claimed in this session")
    sys.monitoring.free_tool_id(sys.monitoring.PROFILER_ID)

    tracer = CallGraphTracer(tmp_path)

    def _boom(tool_id, events):
        raise RuntimeError("setup failed")

    monkeypatch.setattr(sys.monitoring, "set_events", _boom)
    with pytest.raises(RuntimeError):
        tracer.start()
    monkeypatch.undo()

    assert not tracer.active
    # The profiler slot must be free again: claiming it succeeds.
    sys.monitoring.use_tool_id(sys.monitoring.PROFILER_ID, "probe")
    sys.monitoring.free_tool_id(sys.monitoring.PROFILER_ID)


_HOOK_MODULE = """
    def callee():
        pass


    class Receiver:
        def method(self):
            _hook(Receiver.method.__code__, 0)


    def call_method():
        Receiver().method()


    def caller():
        _hook(callee.__code__, 0)


    def outer():
        caller()
"""


def _hook_namespace(tmp_path: Path, tracer: CallGraphTracer) -> dict:
    # Executing the module from a file under the repo root gives its frames
    # in-scope co_filenames, so the callback can be driven directly without
    # claiming the interpreter-wide profiler slot.
    path = tmp_path / "hookmod.py"
    source = textwrap.dedent(_HOOK_MODULE)
    path.write_text(source)
    namespace = {"_hook": tracer._on_py_start}
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def test_callback_aggregates_pairs_and_workloads(tmp_path):
    tracer = CallGraphTracer(tmp_path)
    namespace = _hook_namespace(tmp_path, tracer)

    tracer.set_workload("w1")
    namespace["outer"]()
    namespace["outer"]()
    tracer.set_workload(None)
    namespace["outer"]()

    records = {(r.caller.qualname, r.callee.qualname): r for r in tracer.records()}
    edge = records[("outer", "callee")]
    assert edge.count == 3
    assert edge.workloads == ("w1",)


def test_callback_samples_receiver_types(tmp_path):
    tracer = CallGraphTracer(tmp_path)
    namespace = _hook_namespace(tmp_path, tracer)

    namespace["call_method"]()

    records = {(r.caller.qualname, r.callee.qualname): r for r in tracer.records()}
    edge = records[("call_method", "Receiver.method")]
    assert len(edge.receiver_types) == 1
    assert edge.receiver_types[0].endswith("Receiver")


def test_callback_ignores_out_of_scope_callers(tmp_path):
    tracer = CallGraphTracer(tmp_path)
    namespace = _hook_namespace(tmp_path, tracer)

    # Called from this test file, the caller frame lives outside tmp_path.
    namespace["caller"]()

    assert tracer.records() == []


def test_tracer_write_read_roundtrip(tmp_path):
    package = "dyn_trace_pkg_c"
    _write_package(tmp_path, package)
    tracer = _trace_package(tmp_path, package, workload="w")

    output = tmp_path / "trace.jsonl"
    written = tracer.write(output)
    header, records_iter = read_trace_file(output)
    records = list(records_iter)

    assert written == len(records)
    assert header.repo_root == str(tmp_path)
    in_memory = {
        (r.caller.qualname, r.callee.qualname, r.count) for r in tracer.records()
    }
    round_tripped = {(r.caller.qualname, r.callee.qualname, r.count) for r in records}
    assert in_memory == round_tripped
