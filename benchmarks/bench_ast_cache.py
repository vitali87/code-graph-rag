import statistics
import sys
import time
from collections import OrderedDict
from pathlib import Path

WARMUP_RUNS = 3
BENCH_RUNS = 50


class MockNode:
    __slots__ = ("data",)

    def __init__(self, size: int) -> None:
        self.data = b"\x00" * size


def bench_ordered_dict_insert(count: int, item_size: int) -> float:
    start = time.perf_counter()
    cache: OrderedDict[Path, tuple[MockNode, str]] = OrderedDict()
    for i in range(count):
        key = Path(f"/fake/path/module_{i}.py")
        cache[key] = (MockNode(item_size), "python")
    return time.perf_counter() - start


def bench_ordered_dict_lookup(cache: OrderedDict, keys: list[Path]) -> float:
    start = time.perf_counter()
    for key in keys:
        _ = key in cache
    return time.perf_counter() - start


def bench_ordered_dict_access_lru(cache: OrderedDict, keys: list[Path]) -> float:
    start = time.perf_counter()
    for key in keys:
        if key in cache:
            cache.move_to_end(key)
            _ = cache[key]
    return time.perf_counter() - start


def bench_ordered_dict_eviction(count: int, max_size: int, item_size: int) -> float:
    start = time.perf_counter()
    cache: OrderedDict[Path, tuple[MockNode, str]] = OrderedDict()
    for i in range(count):
        key = Path(f"/fake/path/module_{i}.py")
        cache[key] = (MockNode(item_size), "python")
        while len(cache) > max_size:
            cache.popitem(last=False)
    return time.perf_counter() - start


def bench_getsizeof_overhead(cache: OrderedDict) -> float:
    start = time.perf_counter()
    _ = sum(sys.getsizeof(v) for v in cache.values())
    return time.perf_counter() - start


def run_benchmark(name: str, func, *args) -> dict[str, float]:
    for _ in range(WARMUP_RUNS):
        func(*args)

    times = []
    for _ in range(BENCH_RUNS):
        times.append(func(*args))

    return {
        "name": name,
        "median_ms": statistics.median(times) * 1000,
        "mean_ms": statistics.mean(times) * 1000,
        "stddev_ms": statistics.stdev(times) * 1000 if len(times) > 1 else 0,
        "min_ms": min(times) * 1000,
        "max_ms": max(times) * 1000,
        "p95_ms": sorted(times)[int(len(times) * 0.95)] * 1000,
    }


def print_results(results: list[dict[str, float]]) -> None:
    print(f"\n{'Benchmark':<45} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 115)
    for r in results:
        print(
            f"{r['name']:<45} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def main() -> None:
    configs = [
        (500, 1024),
        (2000, 4096),
        (5000, 8192),
    ]

    for count, item_size in configs:
        print(f"\n{'='*115}")
        print(f"BoundedASTCache Benchmark (entries={count}, item_size={item_size}B)")
        print(f"{'='*115}")

        results = []

        r = run_benchmark(f"insert ({count})", bench_ordered_dict_insert, count, item_size)
        results.append(r)

        cache: OrderedDict[Path, tuple[MockNode, str]] = OrderedDict()
        keys: list[Path] = []
        for i in range(count):
            key = Path(f"/fake/path/module_{i}.py")
            keys.append(key)
            cache[key] = (MockNode(item_size), "python")

        r = run_benchmark(f"lookup ({count})", bench_ordered_dict_lookup, cache, keys)
        results.append(r)

        r = run_benchmark(f"access+LRU ({count})", bench_ordered_dict_access_lru, cache, keys)
        results.append(r)

        max_size = count // 2
        r = run_benchmark(
            f"insert+evict (max={max_size})",
            bench_ordered_dict_eviction, count, max_size, item_size,
        )
        results.append(r)

        r = run_benchmark(f"getsizeof scan ({count})", bench_getsizeof_overhead, cache)
        results.append(r)

        print_results(results)


if __name__ == "__main__":
    main()
