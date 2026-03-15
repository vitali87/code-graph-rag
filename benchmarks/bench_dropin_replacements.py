import hashlib
import json
import os
import statistics
import tempfile
import time
from pathlib import Path

import blake3
import orjson

WARMUP_RUNS = 3
BENCH_RUNS = 30


def generate_graph_data(num_nodes: int, num_rels: int) -> dict:
    nodes = []
    for i in range(num_nodes):
        nodes.append({
            "node_id": i,
            "labels": ["Function" if i % 3 == 0 else "Class" if i % 3 == 1 else "Module"],
            "properties": {
                "qualified_name": f"project.module{i // 100}.Class{i // 10}.method{i}",
                "name": f"method{i}",
                "start_line": i * 10,
                "end_line": i * 10 + 9,
                "docstring": f"Method {i} documentation string with some content" if i % 5 == 0 else None,
                "decorators": ["staticmethod"] if i % 7 == 0 else [],
                "is_exported": i % 4 == 0,
            },
        })

    rels = []
    for i in range(num_rels):
        rels.append({
            "from_id": i % num_nodes,
            "to_id": (i * 7 + 3) % num_nodes,
            "type": "CALLS" if i % 3 == 0 else "DEFINES" if i % 3 == 1 else "IMPORTS",
            "properties": {"weight": i % 10} if i % 5 == 0 else {},
        })

    return {
        "nodes": nodes,
        "relationships": rels,
        "metadata": {
            "total_nodes": num_nodes,
            "total_relationships": num_rels,
            "exported_at": "2026-03-14T10:00:00+00:00",
        },
    }


def generate_snippets(count: int, avg_length: int = 200) -> list[str]:
    import random
    import string
    random.seed(42)
    snippets = []
    for _ in range(count):
        length = avg_length + random.randint(-50, 50)
        snippet = "".join(random.choices(string.ascii_letters + string.digits + " \n\t", k=length))
        snippets.append(snippet)
    return snippets


def create_test_files(directory: str, count: int, avg_size_kb: int) -> list[Path]:
    paths = []
    for i in range(count):
        path = Path(directory) / f"file_{i}.py"
        content = os.urandom(avg_size_kb * 1024)
        path.write_bytes(content)
        paths.append(path)
    return paths


def bench_json_dumps(data: dict) -> float:
    start = time.perf_counter()
    _ = json.dumps(data)
    return time.perf_counter() - start


def bench_orjson_dumps(data: dict) -> float:
    start = time.perf_counter()
    _ = orjson.dumps(data)
    return time.perf_counter() - start


def bench_json_dumps_indent(data: dict) -> float:
    start = time.perf_counter()
    _ = json.dumps(data, indent=2, ensure_ascii=False)
    return time.perf_counter() - start


def bench_orjson_dumps_indent(data: dict) -> float:
    start = time.perf_counter()
    _ = orjson.dumps(data, option=orjson.OPT_INDENT_2)
    return time.perf_counter() - start


def bench_json_loads(json_bytes: bytes) -> float:
    start = time.perf_counter()
    _ = json.loads(json_bytes)
    return time.perf_counter() - start


def bench_orjson_loads(json_bytes: bytes) -> float:
    start = time.perf_counter()
    _ = orjson.loads(json_bytes)
    return time.perf_counter() - start


def bench_sha256_hashing(snippets: list[str]) -> float:
    start = time.perf_counter()
    for s in snippets:
        _ = hashlib.sha256(s.encode()).hexdigest()
    return time.perf_counter() - start


def bench_blake3_hashing(snippets: list[str]) -> float:
    start = time.perf_counter()
    for s in snippets:
        _ = blake3.blake3(s.encode()).hexdigest()
    return time.perf_counter() - start


def bench_sha256_file(files: list[Path]) -> float:
    start = time.perf_counter()
    for f in files:
        hasher = hashlib.sha256()
        with f.open("rb") as fh:
            while chunk := fh.read(8192):
                hasher.update(chunk)
        _ = hasher.hexdigest()
    return time.perf_counter() - start


def bench_blake3_file(files: list[Path]) -> float:
    start = time.perf_counter()
    for f in files:
        hasher = blake3.blake3()
        with f.open("rb") as fh:
            while chunk := fh.read(8192):
                hasher.update(chunk)
        _ = hasher.hexdigest()
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
    print(f"\n{'Benchmark':<50} {'Median':>10} {'Mean':>10} {'StdDev':>10} {'Min':>10} {'Max':>10} {'P95':>10}")
    print("-" * 120)
    for r in results:
        print(
            f"{r['name']:<50} {r['median_ms']:>9.3f}ms {r['mean_ms']:>9.3f}ms "
            f"{r['stddev_ms']:>9.3f}ms {r['min_ms']:>9.3f}ms {r['max_ms']:>9.3f}ms "
            f"{r['p95_ms']:>9.3f}ms"
        )


def print_comparison(baseline: dict[str, float], optimized: dict[str, float]) -> None:
    speedup = baseline["median_ms"] / optimized["median_ms"] if optimized["median_ms"] > 0 else float("inf")
    print(f"  -> Speedup: {speedup:.1f}x (median)")


def main() -> None:
    print("=" * 120)
    print("DROP-IN REPLACEMENT BENCHMARKS: Python stdlib vs Rust-backed alternatives")
    print("=" * 120)

    # --- JSON Serialization ---
    for num_nodes, num_rels in [(1000, 2000), (5000, 10000), (20000, 50000)]:
        print(f"\n{'='*120}")
        print(f"JSON Serialization: stdlib json vs orjson (nodes={num_nodes}, rels={num_rels})")
        print(f"{'='*120}")

        data = generate_graph_data(num_nodes, num_rels)
        json_bytes = json.dumps(data).encode()
        orjson_bytes = orjson.dumps(data)
        print(f"Data size: {len(json_bytes) / 1024:.1f} KB")

        results = []

        r1 = run_benchmark(f"json.dumps compact ({num_nodes}n)", bench_json_dumps, data)
        results.append(r1)
        r2 = run_benchmark(f"orjson.dumps compact ({num_nodes}n)", bench_orjson_dumps, data)
        results.append(r2)

        r3 = run_benchmark(f"json.dumps indented ({num_nodes}n)", bench_json_dumps_indent, data)
        results.append(r3)
        r4 = run_benchmark(f"orjson.dumps indented ({num_nodes}n)", bench_orjson_dumps_indent, data)
        results.append(r4)

        r5 = run_benchmark(f"json.loads ({num_nodes}n)", bench_json_loads, json_bytes)
        results.append(r5)
        r6 = run_benchmark(f"orjson.loads ({num_nodes}n)", bench_orjson_loads, orjson_bytes)
        results.append(r6)

        print_results(results)

        print("\nSpeedups:")
        print(f"  dumps compact: {r1['median_ms'] / r2['median_ms']:.1f}x")
        print(f"  dumps indented: {r3['median_ms'] / r4['median_ms']:.1f}x")
        print(f"  loads: {r5['median_ms'] / r6['median_ms']:.1f}x")

    # --- Hashing: SHA256 vs BLAKE3 ---
    print(f"\n\n{'='*120}")
    print("Hashing: hashlib.sha256 vs blake3 (snippet hashing for EmbeddingCache)")
    print(f"{'='*120}")

    for size in [500, 2000, 10000]:
        snippets = generate_snippets(size)
        print(f"\n--- Snippet count: {size} ---")

        results = []
        r1 = run_benchmark(f"hashlib.sha256 ({size} snippets)", bench_sha256_hashing, snippets)
        results.append(r1)
        r2 = run_benchmark(f"blake3 ({size} snippets)", bench_blake3_hashing, snippets)
        results.append(r2)

        print_results(results)
        print(f"  Speedup: {r1['median_ms'] / r2['median_ms']:.1f}x")

    # --- File Hashing ---
    print(f"\n\n{'='*120}")
    print("File Hashing: SHA256 vs BLAKE3 (incremental build file change detection)")
    print(f"{'='*120}")

    for file_count, avg_size_kb in [(50, 5), (200, 10), (500, 20)]:
        with tempfile.TemporaryDirectory() as tmpdir:
            files = create_test_files(tmpdir, file_count, avg_size_kb)
            total_mb = sum(f.stat().st_size for f in files) / (1024 * 1024)
            print(f"\n--- Files: {file_count}, Total: {total_mb:.1f} MB ---")

            results = []
            r1 = run_benchmark(f"sha256 ({file_count}f, {avg_size_kb}KB avg)", bench_sha256_file, files)
            results.append(r1)
            r2 = run_benchmark(f"blake3 ({file_count}f, {avg_size_kb}KB avg)", bench_blake3_file, files)
            results.append(r2)

            print_results(results)
            print(f"  Speedup: {r1['median_ms'] / r2['median_ms']:.1f}x")


if __name__ == "__main__":
    main()
