# Issue #1480: the benchmark harness reports a p95 that is not the 95th
# percentile at small run counts. Kept with the tests rather than in
# `benchmarks/` because it is a correctness check on the statistic, not a
# benchmark, and must run in the normal suite.
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from benchmarks.harness import run_benchmark  # noqa: E402


def _timings(values: list[float]):
    """A `func` whose successive calls return `values`, after the warmups."""
    remaining = list(values)

    def func() -> float:
        return remaining.pop(0)

    return func


def test_p95_is_not_simply_the_maximum_at_twenty_runs() -> None:
    """`int(n * 0.95)` selects the last index when n is 20, so p95 == max.

    Nearest-rank puts the 95th percentile of twenty samples at rank 19,
    which is index 18. Truncating `20 * 0.95 = 19.0` lands on index 19 -- the
    maximum -- so the harness reports its slowest run as the p95 and the
    metric stops distinguishing a tail from an outlier.

    Measured rather than reasoned: p95 == max at n=10 and n=20, and differs
    at n=40, 50, 100. So the defect is size-dependent, which is why it
    survived: the default `bench_runs=50` does not show it, and a caller
    lowering the count to iterate faster gets a silently wrong statistic
    (CodeRabbit, #1480).

    Drives the shipped `run_benchmark` rather than recomputing the index, so a
    fix that changes the arithmetic without changing the reported value
    would still fail here.
    """
    # 20 samples: 0.001 .. 0.020 seconds, already ascending.
    values = [(i + 1) / 1000 for i in range(20)]
    # `run_benchmark` runs `warmup_runs` first and discards them.
    func = _timings([0.0] * 3 + values)

    result = run_benchmark("p95-shape", func, warmup_runs=3, bench_runs=20)

    assert result["max_ms"] == 20.0, "fixture sanity: the slowest run is 20ms"
    assert result["p95_ms"] != result["max_ms"], (
        f"p95 ({result['p95_ms']}ms) equals max ({result['max_ms']}ms) at 20 "
        "runs; nearest-rank puts it at rank 19 of 20, which is the 19ms sample"
    )
    assert result["p95_ms"] == 19.0, (
        f"nearest-rank p95 of 20 ascending samples is the 19th (19ms), got "
        f"{result['p95_ms']}ms"
    )


def test_p95_is_unchanged_at_the_default_run_count() -> None:
    """The control: at `bench_runs=50` the old and new indices agree.

    `int(50 * 0.95)` and `ceil(50 * 0.95) - 1` are both 47, so the default
    path must report exactly what it always did. Without this, a fix that
    shifted every count by one would pass the test above while silently
    changing every existing benchmark's headline number.
    """
    values = [(i + 1) / 1000 for i in range(50)]
    func = _timings([0.0] * 3 + values)

    result = run_benchmark("p95-default", func, warmup_runs=3, bench_runs=50)

    assert result["p95_ms"] == 48.0, (
        "index 47 of 50 ascending samples is the 48ms one; the default count "
        f"must be unaffected by the fix, got {result['p95_ms']}ms"
    )
