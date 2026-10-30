"""The GraphLoader benchmark builds an export-shaped graph and times the
loader's finders on it. Its rows mean something only if the graph loads as an
export and each finder is timed on lookups that find what they ask for."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks import bench_graph_loader as bench
from codebase_rag.graph_loader import GraphLoader
from codebase_rag.models import GraphNode
from codebase_rag.types_defs import PropertyValue

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {
    "json.loads",
    "GraphLoader.load",
    "find_nodes_by_label",
    "find_node_by_property",
    "get_relationships",
    "summary",
}


class _RecordingLoader(GraphLoader):
    """Keeps every property lookup's answer, which the benchmark discards."""

    def __init__(self, file_path: str) -> None:
        super().__init__(file_path)
        self.answers: list[list[GraphNode]] = []

    def find_node_by_property(
        self, property_name: str, value: PropertyValue
    ) -> list[GraphNode]:
        answer = super().find_nodes_by_property(property_name, value)
        self.answers.append(answer)
        return answer


def _write_graph(tmp_path: Path, num_nodes: int, num_rels: int) -> Path:
    path = tmp_path / "graph.json"
    path.write_text(bench.generate_graph_json(num_nodes, num_rels), encoding="utf-8")
    return path


def test_the_generated_graph_loads_as_an_export(tmp_path: Path) -> None:
    loader = GraphLoader(str(_write_graph(tmp_path, 30, 45)))
    loader.load()

    summary = loader.summary()
    assert summary["total_nodes"] == 30
    assert summary["total_relationships"] == 45
    assert summary["node_labels"] == {"Function": 10, "Class": 10, "Module": 10}
    assert summary["relationship_types"] == {"CALLS": 23, "DEFINES": 22}
    names = [node.properties["qualified_name"] for node in loader.nodes]
    assert len(set(names)) == 30
    assert names[25] == "project.module0.Class2.method25"


def test_relationships_only_join_generated_nodes() -> None:
    graph = json.loads(bench.generate_graph_json(7, 50))
    ids = {node["node_id"] for node in graph["nodes"]}
    assert {rel["from_id"] for rel in graph["relationships"]} <= ids
    assert {rel["to_id"] for rel in graph["relationships"]} <= ids


def test_the_property_probe_times_lookups_that_hit(tmp_path: Path) -> None:
    """Every qualified name the probe asks for is one the generator wrote.

    The probe once spelled the module segment `module{i}` while the generator
    writes `module{n // 100}`, so 99 of its 100 lookups missed and the
    `find_node_by_property` row timed the miss path of the index.
    """
    loader = _RecordingLoader(str(_write_graph(tmp_path, 1000, 10)))
    loader.load()

    elapsed = bench.bench_find_node_by_property(loader)

    assert elapsed >= 0
    assert len(loader.answers) == 100
    misses = [i for i, answer in enumerate(loader.answers) if len(answer) != 1]
    assert misses == [], f"probes {misses[:5]}... found nothing"
    assert loader.answers[42][0].properties["name"] == "method420"


def test_the_relationship_probe_reaches_nodes_with_edges(tmp_path: Path) -> None:
    loader = GraphLoader(str(_write_graph(tmp_path, 20, 40)))
    loader.load()
    # Every node has an edge at these sizes, so the probe never times the
    # empty answer alone.
    assert all(loader.get_relationships_for_node(i) for i in range(20))
    assert bench.bench_get_relationships(loader, 20) >= 0


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 2)
    generate = bench.generate_graph_json
    # The sizes are local to main, so the generator is capped instead; 1000
    # nodes is the smallest size and still every node the probes name.
    monkeypatch.setattr(
        bench,
        "generate_graph_json",
        lambda num_nodes, num_rels: generate(min(num_nodes, 1000), min(num_rels, 50)),
    )

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
    assert out.count("GraphLoader Benchmark") == 3
