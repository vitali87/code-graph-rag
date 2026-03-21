import re
import statistics
import time

WARMUP_RUNS = 3
BENCH_RUNS = 100

SEPARATOR_PATTERN = re.compile(r"[.:]|::")


def generate_qualified_names(count: int) -> list[str]:
    names = []
    modules = ["project", "utils", "core", "api", "services", "models"]
    classes = ["Handler", "Manager", "Factory", "Builder", "Processor", "Resolver"]
    methods = ["process", "handle", "create", "build", "resolve", "validate"]
    for i in range(count):
        mod = modules[i % len(modules)]
        cls = classes[(i // len(modules)) % len(classes)]
        meth = methods[(i // (len(modules) * len(classes))) % len(methods)]
        names.append(f"{mod}.{cls}.sub{i}.{meth}")
    return names


def bench_str_split(names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        _ = name.split(".")
    return time.perf_counter() - start


def bench_str_endswith(names: list[str]) -> float:
    suffixes = [".process", ".handle", ".create", ".build", ".resolve"]
    start = time.perf_counter()
    for name in names:
        for suffix in suffixes:
            _ = name.endswith(suffix)
    return time.perf_counter() - start


def bench_str_startswith(names: list[str]) -> float:
    prefixes = ["project.", "utils.", "core.", "api."]
    start = time.perf_counter()
    for name in names:
        for prefix in prefixes:
            _ = name.startswith(prefix)
    return time.perf_counter() - start


def bench_str_join(names: list[str]) -> float:
    split_names = [name.split(".") for name in names]
    start = time.perf_counter()
    for parts in split_names:
        _ = ".".join(parts)
    return time.perf_counter() - start


def bench_str_replace(names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        _ = name.replace("/", ".")
    return time.perf_counter() - start


def bench_regex_split(names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        _ = SEPARATOR_PATTERN.split(name)
    return time.perf_counter() - start


def bench_str_format(names: list[str]) -> float:
    start = time.perf_counter()
    for name in names:
        _ = f"module.{name}.method"
    return time.perf_counter() - start


def bench_import_distance(names: list[str]) -> float:
    start = time.perf_counter()
    for i in range(0, len(names) - 1, 2):
        caller_parts = names[i].split(".")
        candidate_parts = names[i + 1].split(".")
        common = 0
        for j in range(min(len(caller_parts), len(candidate_parts))):
            if caller_parts[j] == candidate_parts[j]:
                common += 1
            else:
                break
        _ = max(len(caller_parts), len(candidate_parts)) - common
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
    print(f"\n{'Benchmark':<40} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 110)
    for r in results:
        print(
            f"{r['name']:<40} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def main() -> None:
    sizes = [1000, 5000, 20000]

    for size in sizes:
        print(f"\n{'='*110}")
        print(f"String Operations Benchmark (n={size})")
        print(f"{'='*110}")

        names = generate_qualified_names(size)

        results = [
            run_benchmark(f"str.split ({size})", bench_str_split, names),
            run_benchmark(f"str.endswith ({size})", bench_str_endswith, names),
            run_benchmark(f"str.startswith ({size})", bench_str_startswith, names),
            run_benchmark(f"str.join ({size})", bench_str_join, names),
            run_benchmark(f"str.replace ({size})", bench_str_replace, names),
            run_benchmark(f"regex split ({size})", bench_regex_split, names),
            run_benchmark(f"f-string format ({size})", bench_str_format, names),
            run_benchmark(f"import_distance ({size})", bench_import_distance, names),
        ]

        print_results(results)


if __name__ == "__main__":
    main()
