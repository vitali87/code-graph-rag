# Issue #2888: `pytest --cgr-trace` reports the trace it wrote on a line of
# its own. The message was written from `pytest_sessionfinish`, before pytest
# ends the progress line, so under `-q` (or `addopts = -q`) it was glued to
# it: `..  [100%]cgr-trace: wrote 2 call records to cgr-trace.jsonl`.
# Subprocess runs, as in test_dynamic_trace_pytest_plugin.py, so the inner
# session's profiler cannot collide with the outer session's tooling.
from __future__ import annotations

import re

import pytest

SUMMARY = re.compile(r"^cgr-trace: wrote \d+ call records to cgr-trace\.jsonl$")

pytestmark = pytest.mark.slow


@pytest.fixture
def traced(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makepyfile(m="def f(x):\n    return x + 1\n")
    pytester.makepyfile(
        test_m=(
            "from m import f\n\n\n"
            "def test_a():\n    assert f(1) == 2\n\n\n"
            "def test_b():\n    assert f(2) == 3\n"
        )
    )
    return pytester


def _mentions(result: pytest.RunResult) -> list[str]:
    return [line for line in result.outlines if "cgr-trace: wrote" in line]


def test_the_summary_is_its_own_line_under_q(traced: pytest.Pytester) -> None:
    result = traced.runpytest_subprocess("--cgr-trace", "-q")

    result.assert_outcomes(passed=2)
    assert [SUMMARY.match(line) is not None for line in _mentions(result)] == [True]


def test_the_summary_is_its_own_line_under_addopts_q(
    traced: pytest.Pytester,
) -> None:
    traced.makeini("[pytest]\naddopts = -q\n")

    result = traced.runpytest_subprocess("--cgr-trace")

    result.assert_outcomes(passed=2)
    assert [SUMMARY.match(line) is not None for line in _mentions(result)] == [True]


def test_the_progress_line_ends_before_the_summary(traced: pytest.Pytester) -> None:
    result = traced.runpytest_subprocess("--cgr-trace", "-q")

    progress = [line for line in result.outlines if "[100%]" in line]
    assert progress
    assert all(line.rstrip().endswith("[100%]") for line in progress)


# Negative: what must not change.


@pytest.mark.parametrize(
    "args",
    [(), ("-v",), ("-o", "console_output_style=classic")],
    ids=["default", "verbose", "classic"],
)
def test_the_summary_stays_its_own_line_elsewhere(
    traced: pytest.Pytester, args: tuple[str, ...]
) -> None:
    result = traced.runpytest_subprocess("--cgr-trace", *args)

    result.assert_outcomes(passed=2)
    assert [SUMMARY.match(line) is not None for line in _mentions(result)] == [True]


def test_the_summary_comes_before_the_outcome_line(traced: pytest.Pytester) -> None:
    result = traced.runpytest_subprocess("--cgr-trace")

    summary = next(i for i, line in enumerate(result.outlines) if SUMMARY.match(line))
    outcome = next(i for i, line in enumerate(result.outlines) if "2 passed" in line)
    assert summary < outcome


def test_the_trace_file_is_still_written(traced: pytest.Pytester) -> None:
    traced.runpytest_subprocess("--cgr-trace", "-q")

    assert (traced.path / "cgr-trace.jsonl").stat().st_size > 0


def test_no_summary_without_the_flag(traced: pytest.Pytester) -> None:
    result = traced.runpytest_subprocess("-q")

    result.assert_outcomes(passed=2)
    assert _mentions(result) == []
