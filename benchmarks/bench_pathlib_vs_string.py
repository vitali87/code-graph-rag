import os
import statistics
import time
from pathlib import Path, PurePosixPath

WARMUP_RUNS = 3
BENCH_RUNS = 50


def generate_file_paths(repo_root: str, count: int) -> list[str]:
    dirs = ["src", "lib", "utils", "core", "parsers", "services", "tools", "tests"]
    subdirs = ["base", "handlers", "helpers", "models", "schemas", "config"]
    extensions = [".py", ".js", ".ts", ".rs", ".go", ".java", ".cpp"]

    paths = []
    for i in range(count):
        d = dirs[i % len(dirs)]
        sd = subdirs[(i // len(dirs)) % len(subdirs)]
        ext = extensions[(i // (len(dirs) * len(subdirs))) % len(extensions)]
        paths.append(f"{repo_root}/{d}/{sd}/module_{i}{ext}")
    return paths


def generate_skip_patterns() -> list[str]:
    return [
        "node_modules", ".git", "__pycache__", ".venv", "dist", "build",
        ".mypy_cache", ".pytest_cache", ".tox", "egg-info",
    ]


def bench_pathlib_relative_to(paths: list[str], repo_root: str) -> float:
    repo_path = Path(repo_root)
    start = time.perf_counter()
    for p in paths:
        path = Path(p)
        _ = path.relative_to(repo_path)
    return time.perf_counter() - start


def bench_string_removeprefix(paths: list[str], repo_root: str) -> float:
    prefix = repo_root + "/"
    start = time.perf_counter()
    for p in paths:
        _ = p.removeprefix(prefix)
    return time.perf_counter() - start


def bench_os_path_relpath(paths: list[str], repo_root: str) -> float:
    start = time.perf_counter()
    for p in paths:
        _ = os.path.relpath(p, repo_root)
    return time.perf_counter() - start


def bench_pathlib_should_skip(paths: list[str], repo_root: str, skip_patterns: list[str]) -> float:
    repo_path = Path(repo_root)
    skip_set = set(skip_patterns)
    start = time.perf_counter()
    for p in paths:
        path = Path(p)
        try:
            relative = path.relative_to(repo_path)
            parts = relative.parts
            _ = any(part in skip_set for part in parts)
        except ValueError:
            pass
    return time.perf_counter() - start


def bench_string_should_skip(paths: list[str], repo_root: str, skip_patterns: list[str]) -> float:
    prefix = repo_root + "/"
    skip_set = set(skip_patterns)
    start = time.perf_counter()
    for p in paths:
        relative = p.removeprefix(prefix)
        parts = relative.split("/")
        _ = any(part in skip_set for part in parts)
    return time.perf_counter() - start


def bench_pathlib_suffix_check(paths: list[str]) -> float:
    start = time.perf_counter()
    for p in paths:
        path = Path(p)
        _ = path.suffix
    return time.perf_counter() - start


def bench_string_suffix_check(paths: list[str]) -> float:
    start = time.perf_counter()
    for p in paths:
        dot_idx = p.rfind(".")
        _ = p[dot_idx:] if dot_idx >= 0 else ""
    return time.perf_counter() - start


def bench_os_path_splitext(paths: list[str]) -> float:
    start = time.perf_counter()
    for p in paths:
        _, _ = os.path.splitext(p)
    return time.perf_counter() - start


def bench_pathlib_name(paths: list[str]) -> float:
    start = time.perf_counter()
    for p in paths:
        path = Path(p)
        _ = path.name
    return time.perf_counter() - start


def bench_string_name(paths: list[str]) -> float:
    start = time.perf_counter()
    for p in paths:
        slash_idx = p.rfind("/")
        _ = p[slash_idx + 1:] if slash_idx >= 0 else p
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
    print(f"\n{'Benchmark':<55} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 125)
    for r in results:
        print(
            f"{r['name']:<55} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def main() -> None:
    print("=" * 125)
    print("pathlib vs String Operations Benchmark")
    print("This benchmarks the #2 CPU hotspot (13.7% of total runtime)")
    print("=" * 125)

    repo_root = "/Users/developer/projects/large-repo"
    skip_patterns = generate_skip_patterns()

    for count in [1000, 5000, 20000, 59012]:
        print(f"\n{'='*125}")
        print(f"Path count: {count} (59012 = actual profiled call count)")
        print(f"{'='*125}")

        paths = generate_file_paths(repo_root, count)

        results = []

        print("\n--- relative_to vs removeprefix ---")
        r1 = run_benchmark(f"pathlib.relative_to ({count}p)", bench_pathlib_relative_to, paths, repo_root)
        results.append(r1)
        r2 = run_benchmark(f"str.removeprefix ({count}p)", bench_string_removeprefix, paths, repo_root)
        results.append(r2)
        r3 = run_benchmark(f"os.path.relpath ({count}p)", bench_os_path_relpath, paths, repo_root)
        results.append(r3)

        print_results(results)
        print(f"\n  -> pathlib vs str.removeprefix: {r1['median_ms'] / r2['median_ms']:.0f}x slower")
        print(f"  -> pathlib vs os.path.relpath: {r1['median_ms'] / r3['median_ms']:.1f}x slower")

        results = []
        print("\n--- should_skip_path (full function) ---")
        r1 = run_benchmark(f"pathlib should_skip ({count}p)", bench_pathlib_should_skip, paths, repo_root, skip_patterns)
        results.append(r1)
        r2 = run_benchmark(f"string should_skip ({count}p)", bench_string_should_skip, paths, repo_root, skip_patterns)
        results.append(r2)

        print_results(results)
        print(f"\n  -> pathlib vs string: {r1['median_ms'] / r2['median_ms']:.1f}x slower")

        results = []
        print("\n--- Suffix/extension extraction ---")
        r1 = run_benchmark(f"Path.suffix ({count}p)", bench_pathlib_suffix_check, paths)
        results.append(r1)
        r2 = run_benchmark(f"str.rfind ({count}p)", bench_string_suffix_check, paths)
        results.append(r2)
        r3 = run_benchmark(f"os.path.splitext ({count}p)", bench_os_path_splitext, paths)
        results.append(r3)

        print_results(results)
        print(f"\n  -> Path.suffix vs str.rfind: {r1['median_ms'] / r2['median_ms']:.1f}x slower")

        results = []
        print("\n--- Filename extraction ---")
        r1 = run_benchmark(f"Path.name ({count}p)", bench_pathlib_name, paths)
        results.append(r1)
        r2 = run_benchmark(f"str.rfind+slice ({count}p)", bench_string_name, paths)
        results.append(r2)

        print_results(results)
        print(f"\n  -> Path.name vs str: {r1['median_ms'] / r2['median_ms']:.1f}x slower")


if __name__ == "__main__":
    main()
