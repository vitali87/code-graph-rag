import statistics
import time
from collections import defaultdict

from codebase_rag.graph_updater import FunctionRegistryTrie
from codebase_rag.types_defs import NodeType, SimpleNameLookup

WARMUP_RUNS = 3
BENCH_RUNS = 50


def generate_qualified_names(count: int) -> list[str]:
    names = []
    modules = ["project", "utils", "core", "api", "services", "models"]
    classes = ["Handler", "Manager", "Factory", "Builder", "Processor", "Resolver"]
    methods = ["process", "handle", "create", "build", "resolve", "validate", "execute"]
    for i in range(count):
        mod = modules[i % len(modules)]
        cls = classes[(i // len(modules)) % len(classes)]
        meth = methods[(i // (len(modules) * len(classes))) % len(methods)]
        sub = f"sub{i}"
        names.append(f"{mod}.{cls}.{sub}.{meth}")
    return names


def bench_insert(trie: FunctionRegistryTrie, names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        trie.insert(name, NodeType.FUNCTION)
    return time.perf_counter() - start


def bench_lookup(trie: FunctionRegistryTrie, names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        _ = name in trie
    return time.perf_counter() - start


def bench_find_ending_with(trie: FunctionRegistryTrie) -> float:
    suffixes = ["process", "handle", "create", "build", "resolve", "validate", "execute"]
    start = time.perf_counter()
    for suffix in suffixes:
        _ = trie.find_ending_with(suffix)
    return time.perf_counter() - start


def bench_find_with_prefix(trie: FunctionRegistryTrie) -> float:
    prefixes = ["project", "utils", "core", "api", "services", "models"]
    start = time.perf_counter()
    for prefix in prefixes:
        _ = trie.find_with_prefix(prefix)
    return time.perf_counter() - start


def bench_delete(names: list[str]) -> float:
    simple_lookup: SimpleNameLookup = defaultdict(set)
    trie = FunctionRegistryTrie(simple_name_lookup=simple_lookup)
    for name in names:
        trie.insert(name, NodeType.FUNCTION)
        simple_name = name.split(".")[-1]
        simple_lookup[simple_name].add(name)

    start = time.perf_counter()
    for name in names[:len(names) // 4]:
        del trie[name]
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
    print(f"\n{'Benchmark':<35} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 105)
    for r in results:
        print(
            f"{r['name']:<35} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def main() -> None:
    sizes = [1000, 5000, 10000, 50000]

    for size in sizes:
        print(f"\n{'='*105}")
        print(f"FunctionRegistryTrie Benchmark (n={size})")
        print(f"{'='*105}")

        names = generate_qualified_names(size)

        simple_lookup: SimpleNameLookup = defaultdict(set)
        trie = FunctionRegistryTrie(simple_name_lookup=simple_lookup)

        results = []

        r = run_benchmark(f"insert ({size})", bench_insert, trie, names)
        results.append(r)

        for name in names:
            simple_name = name.split(".")[-1]
            simple_lookup[simple_name].add(name)

        r = run_benchmark(f"lookup ({size})", bench_lookup, trie, names)
        results.append(r)

        r = run_benchmark(f"find_ending_with ({size})", bench_find_ending_with, trie)
        results.append(r)

        r = run_benchmark(f"find_with_prefix ({size})", bench_find_with_prefix, trie)
        results.append(r)

        r = run_benchmark(f"delete 25% ({size})", bench_delete, names)
        results.append(r)

        print_results(results)


if __name__ == "__main__":
    main()
