# End-to-end: `pytest --cgr-trace` must trace the session, tag records with
# the test's node id as workload provenance, and write a readable trace file
# at session end (issue #1246). Runs pytest in a subprocess so the inner
# session's sys.monitoring profiler registration cannot collide with the
# outer session's tooling.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.trace.pytest_plugin import _OPT_OUTPUT, _output_path
from codebase_rag.trace.records import read_trace_file


class _StubConfig:
    """Just enough of pytest.Config for output-path resolution."""

    def __init__(self, output: str, workerid: str | None = None) -> None:
        self._output = output
        if workerid is not None:
            self.workerinput = {"workerid": workerid}

    def getoption(self, name: str) -> str:
        assert name == _OPT_OUTPUT
        return self._output


def test_plugin_module_defers_tracer_imports():
    # The pytest11 entry point makes pytest import this module at startup in
    # every session, before coverage tooling initialises and regardless of
    # whether tracing is enabled; the tracer machinery must load only when a
    # hook actually needs it.
    import subprocess
    import sys

    probe = (
        "import sys; import codebase_rag.trace.pytest_plugin; "
        "leaked = [m for m in sys.modules if m in ("
        "'codebase_rag.trace.tracer', 'codebase_rag.trace.records')]; "
        "raise SystemExit(1 if leaked else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, check=False
    )
    assert result.returncode == 0


def test_output_path_is_unchanged_without_xdist():
    assert _output_path(_StubConfig("cgr-trace.jsonl")) == Path("cgr-trace.jsonl")


def test_output_path_gets_worker_suffix_under_xdist():
    # Under pytest-xdist every worker writes at session end; without a
    # per-worker name they would all overwrite the same file.
    assert _output_path(_StubConfig("cgr-trace.jsonl", workerid="gw1")) == Path(
        "cgr-trace-gw1.jsonl"
    )


@pytest.mark.slow
def test_plugin_traces_session_and_tags_workloads(pytester: pytest.Pytester):
    pytester.makepyfile(
        app_under_trace=(
            "def helper():\n    return 1\n\n\ndef entry():\n    return helper()\n"
        )
    )
    pytester.makepyfile(
        test_traced=(
            "import app_under_trace\n"
            "\n"
            "\n"
            "def test_entry():\n"
            "    assert app_under_trace.entry() == 1\n"
        )
    )

    # The plugin auto-loads through the pytest11 entry point; passing -p as
    # well would register the module twice and abort the session.
    result = pytester.runpytest_subprocess("--cgr-trace")

    result.assert_outcomes(passed=1)
    trace_file = pytester.path / "cgr-trace.jsonl"
    assert trace_file.exists()

    header, records_iter = read_trace_file(trace_file)
    records = list(records_iter)
    assert header.repo_root == str(pytester.path)

    by_pair = {(r.caller.qualname, r.callee.qualname): r for r in records}
    edge = by_pair.get(("entry", "helper"))
    assert edge is not None, sorted(by_pair)
    assert edge.count == 1
    assert edge.workloads == ("test_traced.py::test_entry",)


def test_plugin_hooks_run_in_process(pytester: pytest.Pytester):
    # The subprocess variant above proves end-to-end behaviour; this
    # in-process run exercises the hook implementations where coverage can
    # observe them. It needs the profiler slot, so it skips under an outer
    # `--cgr-trace` session.
    import sys

    try:
        sys.monitoring.use_tool_id(sys.monitoring.PROFILER_ID, "probe")
    except ValueError:
        pytest.skip("sys.monitoring PROFILER_ID already claimed in this session")
    sys.monitoring.free_tool_id(sys.monitoring.PROFILER_ID)

    pytester.makepyfile(
        app_in_process=(
            "def helper():\n    return 2\n\n\ndef entry():\n    return helper()\n"
        )
    )
    pytester.makepyfile(
        test_in_process=(
            "import app_in_process\n"
            "\n"
            "\n"
            "def test_entry():\n"
            "    assert app_in_process.entry() == 2\n"
        )
    )

    result = pytester.runpytest_inprocess("--cgr-trace")

    result.assert_outcomes(passed=1)
    header, records_iter = read_trace_file(pytester.path / "cgr-trace.jsonl")
    by_pair = {(r.caller.qualname, r.callee.qualname): r for r in records_iter}
    edge = by_pair.get(("entry", "helper"))
    assert edge is not None, sorted(by_pair)
    assert edge.workloads == ("test_in_process.py::test_entry",)


def test_plugin_is_inert_without_flag(pytester: pytest.Pytester):
    pytester.makepyfile(test_noop="def test_ok():\n    assert True\n")

    result = pytester.runpytest_subprocess()

    result.assert_outcomes(passed=1)
    assert not (pytester.path / "cgr-trace.jsonl").exists()
