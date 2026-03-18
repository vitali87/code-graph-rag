import json
import statistics
import tempfile
import time
from pathlib import Path

WARMUP_RUNS = 3
BENCH_RUNS = 20


def generate_graph_data(num_nodes: int, num_rels: int) -> dict:
    nodes = []
    for i in range(num_nodes):
        nodes.append({
            "id": i,
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


def bench_json_dumps(data: dict) -> float:
    start = time.perf_counter()
    _ = json.dumps(data)
    return time.perf_counter() - start


def bench_json_dumps_indent(data: dict) -> float:
    start = time.perf_counter()
    _ = json.dumps(data, indent=2, ensure_ascii=False)
    return time.perf_counter() - start


def bench_json_loads(json_str: str) -> float:
    start = time.perf_counter()
    _ = json.loads(json_str)
    return time.perf_counter() - start


def bench_json_dump_file(data: dict, path: str) -> float:
    start = time.perf_counter()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return time.perf_counter() - start


def bench_json_load_file(path: str) -> float:
    start = time.perf_counter()
    with open(path, encoding="utf-8") as f:
        _ = json.load(f)
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
        (1000, 2000),
        (5000, 10000),
        (20000, 50000),
    ]

    for num_nodes, num_rels in configs:
        print(f"\n{'='*115}")
        print(f"JSON Serialization Benchmark (nodes={num_nodes}, rels={num_rels})")
        print(f"{'='*115}")

        data = generate_graph_data(num_nodes, num_rels)
        json_str = json.dumps(data)
        json_str_indented = json.dumps(data, indent=2, ensure_ascii=False)
        print(f"Compact JSON: {len(json_str) / 1024:.1f} KB, Indented: {len(json_str_indented) / 1024:.1f} KB")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            json.dump(data, tmp, indent=2, ensure_ascii=False)
            tmp_path = tmp.name

        results = []

        r = run_benchmark(f"json.dumps compact ({num_nodes}n)", bench_json_dumps, data)
        results.append(r)

        r = run_benchmark(f"json.dumps indented ({num_nodes}n)", bench_json_dumps_indent, data)
        results.append(r)

        r = run_benchmark(f"json.loads compact ({num_nodes}n)", bench_json_loads, json_str)
        results.append(r)

        r = run_benchmark(f"json.loads indented ({num_nodes}n)", bench_json_loads, json_str_indented)
        results.append(r)

        r = run_benchmark(f"json.dump to file ({num_nodes}n)", bench_json_dump_file, data, tmp_path)
        results.append(r)

        r = run_benchmark(f"json.load from file ({num_nodes}n)", bench_json_load_file, tmp_path)
        results.append(r)

        print_results(results)

        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
