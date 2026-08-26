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
    """
    corpus = _write_corpus(tmp_path / "repo")

    result = measure_indexing(corpus, project_name="bench_fixture")

    assert result.duration_seconds > 0.0
    assert result.peak_rss_bytes > 0


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
    # A pipe-delimited row, so it can be pasted into the README table.
    assert row.startswith("|") and row.endswith("|")


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
    reading to at least the ballast, while a subprocess measurement is
    unmoved.
    """
    corpus = _write_corpus(tmp_path / "repo", files=1)

    before = measure_indexing(corpus, project_name="bench_fixture")

    ballast = bytearray(192 * 1024 * 1024)
    ballast_size = len(ballast)
    del ballast

    after = measure_indexing(corpus, project_name="bench_fixture")

    # Same corpus, same work: the ballast must not show up in the second
    # reading. Allow generous jitter for allocator noise between two real
    # indexing runs, but far less than the ballast a leak would contribute.
    drift = abs(after.peak_rss_bytes - before.peak_rss_bytes)
    assert drift < ballast_size // 2, (
        f"peak moved by {drift} bytes across a released {ballast_size}-byte "
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
