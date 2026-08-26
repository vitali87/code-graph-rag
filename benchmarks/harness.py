"""Shared timing harness for the standalone benchmark scripts.

Every bench_*.py measured its runs and printed its table with the same two
functions; they live here once. WARMUP_RUNS/BENCH_RUNS stay per-script,
because how many runs a suite needs is a property of that suite.
"""

import statistics
from collections.abc import Callable

# Each measured function returns its own elapsed seconds, so the harness
# only aggregates; it never brackets the call itself.
BenchFunc = Callable[..., float]


def run_benchmark(
    name: str,
    func: BenchFunc,
    *args: object,
    warmup_runs: int = 3,
    bench_runs: int = 50,
) -> dict[str, float]:
    for _ in range(warmup_runs):
        func(*args)

    times = [func(*args) for _ in range(bench_runs)]

    return {
        "name": name,
        "median_ms": statistics.median(times) * 1000,
        "mean_ms": statistics.mean(times) * 1000,
        "stddev_ms": statistics.stdev(times) * 1000 if len(times) > 1 else 0,
        "min_ms": min(times) * 1000,
        "max_ms": max(times) * 1000,
        "p95_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
    }


def print_results(results: list[dict[str, float]], name_width: int = 40) -> None:
    # Six 10-wide metric columns plus their separating spaces; the rule width
    # tracks the name column so every suite's table stays aligned.
    print(
        f"\n{'Benchmark':<{name_width}} {'Median':>10} {'Mean':>10} "
        f"{'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}"
    )
    print("-" * (name_width + 70))
    for r in results:
        print(
            f"{r['name']:<{name_width}} {r['median_ms']:>9.3f}ms "
            f"{r['mean_ms']:>9.3f}ms {r['stddev_ms']:>9.3f}ms "
            f"{r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms {r['p95_ms']:>9.3f}ms"
        )
