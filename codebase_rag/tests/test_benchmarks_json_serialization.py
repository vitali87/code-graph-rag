"""The JSON serialisation benchmark times dumps and loads of a synthetic graph
export. Its compact and indented rows compare like with like only if both
encode the same document, and the file rows only if what is written reads
back whole."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks import bench_json_serialization as bench

_ROW = re.compile(r"^(?P<name>.+?) +(?:\d+\.\d{3}ms *){6}$", re.MULTILINE)
_OPERATIONS = {
    "json.dumps compact",
    "json.dumps indented",
    "json.loads compact",
    "json.loads indented",
    "json.dump to file",
    "json.load from file",
}


def test_the_graph_has_the_requested_size_and_shape() -> None:
    data = bench.generate_graph_data(15, 30)
    assert len(data["nodes"]) == 15
    assert len(data["relationships"]) == 30
    assert data["metadata"]["total_nodes"] == 15
    assert data["metadata"]["total_relationships"] == 30
    assert [n["id"] for n in data["nodes"]] == list(range(15))
    # The optional properties vary across nodes, as an export's do, so the
    # encoder meets None, empty lists and booleans.
    docstrings = [n["properties"]["docstring"] for n in data["nodes"]]
    assert docstrings.count(None) == 12
    assert {r["type"] for r in data["relationships"]} == {
        "CALLS",
        "DEFINES",
        "IMPORTS",
    }
    weighted = [r for r in data["relationships"] if r["properties"]]
    assert len(weighted) == 6


def test_compact_and_indented_encode_the_same_document() -> None:
    data = bench.generate_graph_data(10, 20)
    compact = json.dumps(data)
    indented = json.dumps(data, indent=2, ensure_ascii=False)
    assert compact != indented
    assert json.loads(compact) == json.loads(indented) == data


def test_the_file_rows_read_back_what_they_wrote(tmp_path: Path) -> None:
    data = bench.generate_graph_data(8, 12)
    path = str(tmp_path / "graph.json")
    assert bench.bench_json_dump_file(data, path) >= 0
    assert json.loads(Path(path).read_text(encoding="utf-8")) == data
    assert bench.bench_json_load_file(path) >= 0


def test_the_in_memory_rows_report_elapsed_seconds() -> None:
    data = bench.generate_graph_data(4, 4)
    for elapsed in (
        bench.bench_json_dumps(data),
        bench.bench_json_dumps_indent(data),
        bench.bench_json_loads(json.dumps(data)),
    ):
        assert 0 <= elapsed < 1


def test_main_times_every_operation_at_every_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(bench, "WARMUP_RUNS", 0)
    monkeypatch.setattr(bench, "BENCH_RUNS", 1)
    generate = bench.generate_graph_data
    # main's sizes are local to it (up to 20000 nodes), so the generator is
    # capped instead.
    monkeypatch.setattr(
        bench,
        "generate_graph_data",
        lambda num_nodes, num_rels: generate(min(num_nodes, 60), min(num_rels, 120)),
    )

    bench.main()

    out = capsys.readouterr().out
    names = [match["name"] for match in _ROW.finditer(out)]
    assert len(names) == 3 * len(_OPERATIONS)
    assert {name.rsplit(" (", 1)[0] for name in names} == _OPERATIONS
