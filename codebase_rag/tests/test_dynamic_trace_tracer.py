# The sys.monitoring tracer must observe Python-to-Python calls scoped to a
# repository root, including edges static analysis cannot see (dispatch through
# a registry dict) and the concrete receiver type behind an inherited method
# call (issue #1246).

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.tracer import CallGraphTracer

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
        tracer.start()
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
