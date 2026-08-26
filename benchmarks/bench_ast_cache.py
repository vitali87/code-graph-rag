import sys
import time
from collections import OrderedDict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

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
    return harness.run_benchmark(
        name, func, *args, warmup_runs=WARMUP_RUNS, bench_runs=BENCH_RUNS
    )


def print_results(results: list[dict[str, float]]) -> None:
    harness.print_results(results, 45)


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
