"""Tests for the indexing benchmark harness (issue #1374, deliverable 4).

The benchmark's output is a README row that outlives the run producing it, so
the properties worth pinning are the ones that make a published number
trustworthy: that it measures the real pipeline, that it reports what was
measured alongside the measurement, and that it refuses to emit a number it
could not actually take.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from benchmarks.bench_indexing import (
    RSS_UNAVAILABLE,
    IndexingMeasurement,
    format_markdown_row,
    measure_indexing,
)


def _write_corpus(root: Path, files: int = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(files):
        (root / f"mod_{i}.py").write_text(
            f"def fn_{i}(x):\n    return helper_{i}(x)\n\n\ndef helper_{i}(x):\n    return x\n"
        )
    return root


def test_measure_indexing_reports_real_graph_counts(tmp_path: Path) -> None:
    """The counts must come from indexing, not from a placeholder.

    A benchmark that reports zeroes on a corpus with content would look like a
    working harness on an empty repo, so the assertion is that the numbers
    reflect the fixture rather than merely that fields exist.
    """
    corpus = _write_corpus(tmp_path / "repo", files=3)

    result = measure_indexing(corpus, project_name="bench_fixture")

    assert isinstance(result, IndexingMeasurement)
    # 3 modules x 2 functions each; the exact total depends on the structural
    # nodes cgr also emits, so assert the floor rather than an exact count that
    # would break on unrelated schema additions.
    assert result.node_count >= 6, result.node_count
    assert result.edge_count > 0, result.edge_count
    assert result.file_count == 3, result.file_count


def test_measure_indexing_records_wall_clock_and_memory(tmp_path: Path) -> None:
    """Duration and peak memory must be measured, not defaulted.

    Zero would be indistinguishable from "the timer was never started", which
    is the failure this asserts against.

    Memory has a third legitimate state. Where `resource` is unavailable
    (Windows) the benchmark reports `RSS_UNAVAILABLE` deliberately, so the
    assertion admits the sentinel while still rejecting 0 -- what must never
    appear is a value that reads as a measurement without being one.

    An earlier version asserted `> 0` unconditionally and contradicted the
    module's own Windows path: the fix landed in the implementation and not in
    the test, and every local run passed because macOS has `resource` and
    never takes that branch.
    """
    corpus = _write_corpus(tmp_path / "repo")

    result = measure_indexing(corpus, project_name="bench_fixture")

    assert result.duration_seconds > 0.0
    assert result.peak_rss_bytes == RSS_UNAVAILABLE or result.peak_rss_bytes > 0


def test_unavailable_memory_renders_as_not_a_measurement() -> None:
    """The sentinel must never reach a published table as a number.

    Paired with the assertion above: that one permits `RSS_UNAVAILABLE` to
    exist, so this one pins what it must look like when rendered. Without it,
    admitting the sentinel would widen the contract with nothing checking the
    consequence -- a row reading "-1 MiB", or worse "0 MiB", looks measured.
    """
    result = IndexingMeasurement(
        corpus_name="corpus",
        corpus_path="/tmp/corpus",
        cgr_version="0.0.0",
        file_count=1,
        node_count=1,
        edge_count=0,
        duration_seconds=1.0,
        peak_rss_bytes=RSS_UNAVAILABLE,
    )

    row = format_markdown_row(result)

    assert "n/a" in row
    assert "-1" not in row
    assert "0 MiB" not in row


def test_an_empty_corpus_is_refused_rather_than_reported_as_zero(
    tmp_path: Path,
) -> None:
    """A corpus with no indexable files cannot produce a meaningful row.

    Reporting 0 files in 0.01s would publish a number that looks like a fast
    index rather than like a benchmark that measured nothing -- the
    silent-smaller-than-truth shape this repo keeps hitting.
    """
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ValueError, match="no indexable files"):
        measure_indexing(empty, project_name="bench_fixture")


def test_the_markdown_row_carries_what_was_measured(tmp_path: Path) -> None:
    """A number without its corpus and version is not reproducible.

    The issue is explicit that "the credibility of the table is the
    reproducibility of its cells", so the row must name the corpus and the
    version it came from rather than the bare timing.
    """
    corpus = _write_corpus(tmp_path / "repo")
    result = measure_indexing(corpus, project_name="bench_fixture")

    row = format_markdown_row(result)

    assert result.corpus_name in row
    assert result.cgr_version in row
    assert str(result.file_count) in row
    # A pipe-delimited row, so it can be pasted into the README table. Split
    # rather than combined with `and`: a composite failure cannot say which
    # end is malformed, and a row missing its trailing pipe renders as broken
    # markdown in a way the leading-pipe check would not reveal.
    assert row.startswith("|"), row
    assert row.endswith("|"), row


def test_peak_memory_is_local_to_the_run_not_the_process(tmp_path: Path) -> None:
    """A prior allocation in this process must not inflate the reported peak.

    `ru_maxrss` is a process high-water mark, so a benchmark reading it
    directly attributes any earlier allocation to the indexing run. Greptile
    reproduced exactly that: after a released 128 MiB buffer, a one-file index
    reported the stale 128 MiB peak.

    The property is INVARIANCE to a prior parent allocation, not an absolute
    ceiling. An earlier draft asserted `peak < ballast_size` and failed on a
    correct implementation, because a child that imports the parsers
    legitimately peaks well above any ballast this test would allocate. That
    threshold measured "is the run small" when the question is "does the
    parent's history leak in".

    Allocating and releasing the buffer BETWEEN the two measurements is what
    gives the test its teeth: in-process `ru_maxrss` would raise the second
    reading to at least the ballast, while a subprocess measurement is not
    raised by it (it still drifts either way with host load; the assertion
    below is one-sided for that reason).
    """
    corpus = _write_corpus(tmp_path / "repo", files=1)

    before = measure_indexing(corpus, project_name="bench_fixture")
    if before.peak_rss_bytes == RSS_UNAVAILABLE:
        # Windows: both readings are the sentinel, so the drift check would
        # compare -1 with -1 and pass without exercising anything. Skipping is
        # honest; a vacuous pass would report coverage this platform has not
        # got.
        pytest.skip("peak RSS unavailable on this platform")

    ballast = bytearray(192 * 1024 * 1024)
    ballast_size = len(ballast)
    del ballast

    after = measure_indexing(corpus, project_name="bench_fixture")

    # Same corpus, same work: the ballast must not show up in the second
    # reading. DIRECTIONAL, not absolute: a leak of the parent's history can
    # only RAISE the second reading (the ballast was allocated between the
    # two), while the noise between two real subprocess runs on a loaded host
    # -- allocator behaviour, page-cache pressure, scheduling -- moves it
    # either way. `abs()` made a second run that happened to peak LOWER fail
    # for a reason that is not the defect this test exists to catch, and it
    # did, on the same commit minutes apart under a load average above 30
    # (issue #1643). Generous jitter is still allowed upward, but far less
    # than the ballast a leak would contribute.
    drift = after.peak_rss_bytes - before.peak_rss_bytes
    assert drift < ballast_size // 2, (
        f"peak rose by {drift} bytes across a released {ballast_size}-byte "
        "parent allocation; the reported peak is tracking the parent process "
        "rather than the indexing run"
    )


def test_the_benchmark_imports_where_resource_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no `resource` module, and this repo runs Windows CI.

    An unconditional top-level `import resource` makes the module unimportable
    there -- collection fails before any argument handling or measurement.
    """
    import builtins
    import importlib

    real_import = builtins.__import__

    def _no_resource(name, *args, **kwargs):
        if name == "resource":
            raise ModuleNotFoundError("No module named 'resource'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_resource)
    monkeypatch.delitem(sys.modules, "benchmarks.bench_indexing", raising=False)
    monkeypatch.delitem(sys.modules, "resource", raising=False)

    module = importlib.import_module("benchmarks.bench_indexing")

    assert module.measure_indexing is not None


def test_vmhwm_is_parsed_from_proc_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Linux peak comes from VmHWM, parsed in kB and returned in bytes.

    macOS has no /proc, so this path cannot run here and CI Linux is the only
    place it executes for real -- exactly the gap that let the previous
    ru_maxrss bug reach CI green on this machine. Parsing a synthetic status
    file exercises it everywhere.
    """
    import benchmarks.bench_indexing as module

    status = tmp_path / "status"
    status.write_text(
        "Name:\tpython3\nVmPeak:\t 900000 kB\nVmHWM:\t  123456 kB\nVmRSS:\t 100000 kB\n"
    )

    real_open = open

    def _fake_open(path, *args, **kwargs):
        if path == "/proc/self/status":
            return real_open(status, *args, **kwargs)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fake_open)

    assert module._proc_peak_rss_bytes() == 123456 * 1024


def test_a_missing_proc_status_falls_back_rather_than_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No /proc is not the same as "cannot measure".

    macOS resets the high-water mark across exec, so ru_maxrss is already
    child-local there; the fallback must engage rather than reporting the
    unavailable sentinel and losing a figure the platform can supply.
    """
    import benchmarks.bench_indexing as module

    def _no_proc(path, *args, **kwargs):
        raise FileNotFoundError(path)

    monkeypatch.setattr("builtins.open", _no_proc)

    assert module._proc_peak_rss_bytes() is None


def test_linux_without_proc_reports_unavailable_not_ru_maxrss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Linux an unreadable /proc must NOT fall back to ru_maxrss.

    `ru_maxrss` survives fork/exec on Linux, so the fallback would report the
    parent's high-water mark as the child's peak -- reinstating the exact bug
    the subprocess isolation exists to prevent. Greptile measured a released
    192 MiB parent allocation moving the fallback's answer by 199,208,960
    bytes for an identical one-file workload.

    macOS cannot execute this path (it has no /proc and its ru_maxrss IS
    child-local), so the platform is faked. That is the point: the previous
    version of this fallback was wrong on Linux and every local run passed.
    """
    import benchmarks.bench_indexing as module

    monkeypatch.setattr(module, "_proc_peak_rss_bytes", lambda: None)
    monkeypatch.setattr(module.sys, "platform", "linux")

    assert module._self_peak_rss_bytes() == RSS_UNAVAILABLE


def test_non_linux_without_proc_still_uses_ru_maxrss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS keeps the fallback, because there it is genuinely child-local.

    Paired with the test above so the Linux guard cannot be implemented by
    disabling the fallback everywhere -- that would lose a real measurement on
    the platform where it works.
    """
    import benchmarks.bench_indexing as module

    if module.resource is None:  # pragma: no cover - Windows
        pytest.skip("no resource module on this platform")

    monkeypatch.setattr(module, "_proc_peak_rss_bytes", lambda: None)
    monkeypatch.setattr(module.sys, "platform", "darwin")

    assert module._self_peak_rss_bytes() > 0


def test_direct_script_execution_resolves_the_module_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`python benchmarks/bench_indexing.py` must not die on `__spec__`.

    Running a file directly sets `__spec__` to None, so reading
    `__spec__.name` raised AttributeError before the child was ever spawned --
    the benchmark failed on its most obvious invocation while working fine
    under `-m`, which is why every local run and every CI run passed.
    """
    import benchmarks.bench_indexing as module

    monkeypatch.setitem(module.__dict__, "__spec__", None)

    assert module._module_name() == "benchmarks.bench_indexing"


def test_module_execution_uses_the_real_spec_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under `-m` the real spec name wins over the literal fallback.

    Paired with the test above so the fix cannot be implemented by always
    returning the hardcoded string -- that would work today and break the
    moment the module moves.
    """
    import benchmarks.bench_indexing as module

    class _Spec:
        name = "some.relocated.module"

    monkeypatch.setitem(module.__dict__, "__spec__", _Spec())

    assert module._module_name() == "some.relocated.module"
