# Xdebug computerized traces (trace_format=1) record every call with the
# concrete receiver class and the call site; the converter must turn entry
# rows into exact interchange call records, seeing through require/include
# and internal frames, and keeping module toplevels addressable
# (issue #1253).

from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from codebase_rag import constants as cs
from codebase_rag.trace.records import read_trace_file
from codebase_rag.trace.xdebug import convert_xdebug_trace


def _php_with_xdebug() -> str | None:
    """The php interpreter path when it can load Xdebug, else None.

    Evaluated at import (collection) time, so the probe is bounded by a finite
    timeout: a stalled interpreter must degrade to "unavailable", never hang
    pytest collection.
    """
    php = shutil.which("php")
    if php is None:
        return None
    try:
        probe = subprocess.run(
            [php, "-r", 'echo extension_loaded("xdebug") ? "yes" : "no";'],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return php if probe.stdout.strip() == "yes" else None


_HEADER = "Version: 3.5.3\nFile format: 4\nTRACE START [2026-08-14 02:07:42]\n"
_FOOTER = "\t\t\t0.001855\t479056\nTRACE END   [2026-08-14 02:07:42]\n"


def _entry(level, fn_id, name, user_defined, include_file, filename, line):
    return (
        f"{level}\t{fn_id}\t0\t0.001\t480000\t{name}\t{user_defined}\t"
        f"{include_file}\t{filename}\t{line}\t0"
    )


def _exit(level, fn_id):
    return f"{level}\t{fn_id}\t1\t0.002\t480000"


def _write(tmp_path, rows):
    trace_path = tmp_path / "run.xt"
    trace_path.write_text(_HEADER + "\n".join(rows) + "\n" + _FOOTER)
    return trace_path


def _convert(tmp_path, rows, workload=None):
    output = tmp_path / "trace.jsonl"
    count = convert_xdebug_trace(
        _write(tmp_path, rows), output=output, workload=workload
    )
    header, records = read_trace_file(output)
    return count, header, list(records)


def _sample_rows(repo):
    main = f"{repo}/main.php"
    registry = f"{repo}/src/Registry.php"
    return [
        _entry(1, 0, "{main}", 1, "", main, 0),
        # require pulls in a file; it is glue, not an edge endpoint.
        _entry(2, 1, "require", 1, registry, main, 3),
        _exit(2, 1),
        _entry(2, 2, "describeAnimal", 1, "", main, 20),
        _entry(3, 3, r"App\Services\Dog->sound", 1, "", main, 13),
        _exit(3, 3),
        _exit(2, 2),
        _entry(2, 4, "describeAnimal", 1, "", main, 20),
        _entry(3, 5, r"App\Services\Cat->sound", 1, "", main, 13),
        _exit(3, 5),
        _exit(2, 4),
        _entry(2, 6, r"App\Services\Registry::handle", 1, "", main, 23),
        # An internal frame (call_user_func) between two project frames.
        _entry(3, 7, "call_user_func", 0, "", registry, 16),
        _entry(4, 8, r"App\Services\Registry::greet", 1, "", registry, 16),
        _exit(4, 8),
        _exit(3, 7),
        _exit(2, 6),
        _exit(1, 0),
    ]


def test_converts_calls_with_exact_counts(tmp_path):
    repo = tmp_path.as_posix()
    count, header, records = _convert(tmp_path, _sample_rows(repo))

    assert header.language == cs.TRACE_LANGUAGE_PHP
    assert count == len(records)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert edges[("describeAnimal", r"App\Services\Dog->sound")].count == 1
    assert edges[("{main}", "describeAnimal")].count == 2


def test_internal_frames_are_walked_through(tmp_path):
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    edges = {(r.caller.qualname, r.callee.qualname) for r in records}
    assert (
        r"App\Services\Registry::handle",
        r"App\Services\Registry::greet",
    ) in edges
    assert not any("call_user_func" in pair for edge in edges for pair in edge)


def test_require_frames_never_become_endpoints(tmp_path):
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    for record in records:
        assert record.caller.qualname != "require"
        assert record.callee.qualname != "require"


def test_toplevel_frame_keeps_its_defining_file(tmp_path):
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    toplevel_edges = [r for r in records if r.caller.qualname == "{main}"]
    assert toplevel_edges
    for record in toplevel_edges:
        assert record.caller.path == f"{repo}/main.php"


def test_call_sites_annotate_caller_frames(tmp_path):
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    dispatch = edges[("describeAnimal", r"App\Services\Dog->sound")]
    # The call happened at main.php:13, inside describeAnimal's span.
    assert dispatch.caller.path == f"{repo}/main.php"
    assert dispatch.caller.line == 13


def test_callee_defining_files_are_recovered_from_child_call_sites(tmp_path):
    # Xdebug entry rows carry only the call site, never where the callee is
    # defined; but a callee that itself calls something reveals its own file
    # through its child's call site. handle's child (greet's entry) sits in
    # Registry.php, so the main->handle edge learns handle's defining file
    # and line, enabling span-based resolution against path-derived qns.
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    handle = edges[("{main}", r"App\Services\Registry::handle")]
    assert handle.callee.path == f"{repo}/src/Registry.php"
    assert handle.callee.line == 16


def test_instance_calls_capture_the_concrete_receiver_class(tmp_path):
    # Xdebug names an instance call with the runtime class, so the converter
    # records which implementation ran; static calls carry no receiver type.
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(tmp_path, _sample_rows(repo))

    edges = {(r.caller.qualname, r.callee.qualname): r for r in records}
    assert edges[("describeAnimal", r"App\Services\Dog->sound")].receiver_types == (
        "Dog",
    )
    assert edges[("describeAnimal", r"App\Services\Cat->sound")].receiver_types == (
        "Cat",
    )
    # A static call is not dynamic dispatch: no receiver type.
    handle = edges[("{main}", r"App\Services\Registry::handle")]
    assert handle.receiver_types == ()


def test_workload_label_lands_on_every_record(tmp_path):
    repo = tmp_path.as_posix()
    _count, _header, records = _convert(
        tmp_path, _sample_rows(repo), workload="phpunit"
    )

    assert records
    for record in records:
        assert record.workloads == ("phpunit",)


def test_malformed_trace_is_rejected(tmp_path):
    trace_path = tmp_path / "broken.xt"
    trace_path.write_text("not an xdebug trace\n")

    with pytest.raises(ValueError):
        convert_xdebug_trace(trace_path, output=tmp_path / "out.jsonl")


_php = _php_with_xdebug()


@pytest.mark.slow
@pytest.mark.skipif(_php is None, reason="php with the Xdebug extension is unavailable")
def test_live_xdebug_trace_captures_polymorphic_dispatch(tmp_path):
    # dispatch(Animal) calls $a->sound() through the interface; static analysis
    # cannot know which implementation runs. The trace records the concrete
    # Dog/Cat receiver on each edge, and a trait method called on the using
    # class is captured too.
    (tmp_path / "app.php").write_text(
        textwrap.dedent("""\
            <?php
            interface Animal { public function sound(): string; }
            class Dog implements Animal { public function sound(): string { return "woof"; } }
            class Cat implements Animal { public function sound(): string { return "meow"; } }
            trait TGreeter { public function greet(): string { return get_class($this); } }
            class Service { use TGreeter; }
            function dispatch(Animal $a): string { return $a->sound(); }
            function run(): void {
                foreach ([new Dog(), new Cat(), new Dog()] as $a) { dispatch($a); }
                (new Service())->greet();
            }
            run();
            """)
    )
    assert _php is not None
    subprocess.run(
        [
            _php,
            "-d",
            "xdebug.mode=trace",
            "-d",
            "xdebug.start_with_request=yes",
            "-d",
            "xdebug.trace_format=1",
            "-d",
            "xdebug.output_dir=.",
            "-d",
            "xdebug.trace_output_name=run",
            "app.php",
        ],
        check=True,
        capture_output=True,
        cwd=tmp_path,
    )
    trace = next(tmp_path.glob("run.xt*"))
    output = tmp_path / "trace.jsonl"
    count = convert_xdebug_trace(trace, output=output, workload="phpunit")

    assert count > 0
    _header, records_iter = read_trace_file(output)
    edges = {(r.caller.qualname, r.callee.qualname): r for r in records_iter}
    dog = edges[("dispatch", "Dog->sound")]
    assert dog.count == 2
    assert dog.receiver_types == ("Dog",)
    assert edges[("dispatch", "Cat->sound")].receiver_types == ("Cat",)
    # The trait method greet() called on the using class is observed too.
    assert edges[("run", "Service->greet")].receiver_types == ("Service",)
