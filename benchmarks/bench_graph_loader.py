import json
import sys
import tempfile
import time
from pathlib import Path

from codebase_rag.graph_loader import GraphLoader

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness  # noqa: E402

WARMUP_RUNS = 2
BENCH_RUNS = 20


def generate_graph_json(num_nodes: int, num_rels: int) -> str:
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
            },
        })

    rels = []
    for i in range(num_rels):
        rels.append({
            "from_id": i % num_nodes,
            "to_id": (i * 7 + 3) % num_nodes,
            "type": "CALLS" if i % 2 == 0 else "DEFINES",
            "properties": {},
        })

    graph = {
        "nodes": nodes,
        "relationships": rels,
        "metadata": {
            "total_nodes": num_nodes,
            "total_relationships": num_rels,
        },
    }
    return json.dumps(graph)


def bench_json_parse(json_str: str) -> float:
    start = time.perf_counter()
    _ = json.loads(json_str)
    return time.perf_counter() - start


def bench_graph_load(file_path: str) -> float:
    start = time.perf_counter()
    loader = GraphLoader(file_path)
    loader.load()
    return time.perf_counter() - start


def bench_find_nodes_by_label(loader: GraphLoader) -> float:
    labels = ["Function", "Class", "Module"]
    start = time.perf_counter()
    for label in labels:
        _ = loader.find_nodes_by_label(label)
    return time.perf_counter() - start


def bench_find_node_by_property(loader: GraphLoader) -> float:
    start = time.perf_counter()
    for i in range(100):
        qn = f"project.module{i}.Class{i * 10 // 10}.method{i * 10}"
        _ = loader.find_node_by_property("qualified_name", qn)
    return time.perf_counter() - start


def bench_get_relationships(loader: GraphLoader, num_nodes: int) -> float:
    start = time.perf_counter()
    for i in range(min(500, num_nodes)):
        _ = loader.get_relationships_for_node(i)
    return time.perf_counter() - start


def bench_summary(loader: GraphLoader) -> float:
    start = time.perf_counter()
    _ = loader.summary()
    return time.perf_counter() - start


def run_benchmark(name: str, func, *args) -> dict[str, float]:
    return harness.run_benchmark(
        name, func, *args, warmup_runs=WARMUP_RUNS, bench_runs=BENCH_RUNS
    )


def print_results(results: list[dict[str, float]]) -> None:
    harness.print_results(results, 40)


def main() -> None:
    configs = [
        (1000, 2000),
        (5000, 10000),
        (20000, 50000),
    ]

    for num_nodes, num_rels in configs:
        print(f"\n{'='*110}")
        print(f"GraphLoader Benchmark (nodes={num_nodes}, rels={num_rels})")
        print(f"{'='*110}")

        json_str = generate_graph_json(num_nodes, num_rels)
        print(f"JSON size: {len(json_str) / 1024:.1f} KB")

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as tmp:
            tmp.write(json_str)
            tmp_path = tmp.name

        results = []

        r = run_benchmark(f"json.loads ({num_nodes}n)", bench_json_parse, json_str)
        results.append(r)

        r = run_benchmark(f"GraphLoader.load ({num_nodes}n)", bench_graph_load, tmp_path)
        results.append(r)

        loader = GraphLoader(tmp_path)
        loader.load()

        r = run_benchmark(f"find_nodes_by_label ({num_nodes}n)", bench_find_nodes_by_label, loader)
        results.append(r)

        r = run_benchmark(f"find_node_by_property ({num_nodes}n)", bench_find_node_by_property, loader)
        results.append(r)

        r = run_benchmark(f"get_relationships ({num_nodes}n)", bench_get_relationships, loader, num_nodes)
        results.append(r)

        r = run_benchmark(f"summary ({num_nodes}n)", bench_summary, loader)
        results.append(r)

        print_results(results)

        Path(tmp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
