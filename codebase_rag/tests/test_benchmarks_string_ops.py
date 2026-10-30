"""The string-operations benchmark times the qualified-name handling the call
resolver leans on. Its rows only compare like with like if the regex split it
imports from the resolver cuts the generated names where `str.split` does, and
the suite only stays useful if main still runs."""

from __future__ import annotations

import re

import pytest

from benchmarks import bench_string_ops as bench
from codebase_rag.parsers.call_site import SEPARATOR_PATTERN

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {
    "str.split",
    "str.endswith",
    "str.startswith",
    "str.join",
    "str.replace",
    "regex split",
    "f-string format",
    "import_distance",
}


def test_names_are_unique_four_segment_qualified_names() -> None:
    names = bench.generate_qualified_names(300)
    assert len(set(names)) == 300
    assert all(len(name.split(".")) == 4 for name in names)
    assert names[0] == "project.Handler.sub0.process"
    # The method segment moves on every 36 names, so 300 reach every method.
    assert {name.rsplit(".", 1)[-1] for name in names} == {
        "process",
        "handle",
        "create",
        "build",
        "resolve",
        "validate",
    }


def test_the_regex_split_cuts_where_str_split_does() -> None:
    for name in bench.generate_qualified_names(50):
        assert SEPARATOR_PATTERN.split(name) == name.split(".")


def test_every_timed_operation_reports_elapsed_seconds() -> None:
    names = bench.generate_qualified_names(20)
    for timed in (
        bench.bench_str_split,
        bench.bench_str_endswith,
        bench.bench_str_startswith,
        bench.bench_str_join,
        bench.bench_str_replace,
        bench.bench_regex_split,
        bench.bench_str_format,
        bench.bench_import_distance,
    ):
        assert 0 <= timed(names) < 1, timed.__name__


def test_import_distance_handles_an_odd_count_and_no_names() -> None:
    # It pairs neighbours, so a trailing unpaired name must not index past
    # the end.
    assert bench.bench_import_distance(bench.generate_qualified_names(3)) >= 0
    assert bench.bench_import_distance([]) >= 0


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # main's sizes are small enough already; one timed run each is enough.
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
