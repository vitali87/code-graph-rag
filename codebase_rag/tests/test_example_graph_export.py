"""`examples/graph_export_example.py` is the script the graph-export guide
tells users to run on `cgr export` output. It runs here on a small export, so
a change to the loader's API or the export's shape breaks the suite rather
than the first reader who copies the command."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import typer
from loguru import logger
from typer.testing import CliRunner

from examples import graph_export_example as example

_FUNCTIONS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta"]


def _node(node_id: int, label: str, name: str | None) -> dict[str, object]:
    properties: dict[str, object] = {"qualified_name": f"proj.mod.{node_id}"}
    if name is not None:
        properties["name"] = name
    return {"node_id": node_id, "labels": [label], "properties": properties}


def _export(tmp_path: Path, *, metadata: bool = True) -> Path:
    nodes = [_node(0, "Module", "mod")]
    nodes += [_node(i + 1, "Function", name) for i, name in enumerate(_FUNCTIONS)]
    nodes += [_node(8, "Class", "Shape"), _node(9, "Class", None)]
    rels = [
        {"from_id": 0, "to_id": i, "type": "DEFINES", "properties": {}}
        for i in range(1, 10)
    ]
    rels.append({"from_id": 1, "to_id": 2, "type": "CALLS", "properties": {}})
    graph: dict[str, object] = {"nodes": nodes, "relationships": rels}
    if metadata:
        graph["metadata"] = {
            "total_nodes": len(nodes),
            "total_relationships": len(rels),
            "exported_at": "2026-10-30T12:00:00+00:00",
        }
    path = tmp_path / "exported_graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    return path


@pytest.fixture
def messages() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    yield lines
    logger.remove(sink)


def _cli() -> typer.Typer:
    # What `typer.run(main)` builds when the script runs.
    app = typer.Typer()
    app.command()(example.main)
    return app


def test_the_analysis_reports_counts_and_examples(
    tmp_path: Path, messages: list[str]
) -> None:
    example.main(_export(tmp_path))

    assert "   Total nodes: 10" in messages
    assert "   Total relationships: 10" in messages
    assert "   Exported at: 2026-10-30T12:00:00+00:00" in messages
    assert "   Function: 7 nodes" in messages
    assert "   DEFINES: 9 relationships" in messages
    assert "   CALLS: 1 relationships" in messages
    assert "Found 7 'Function' nodes." in messages
    listed = [m.removeprefix("      - ") for m in messages if m.startswith("      - ")]
    # Five examples per label, then a count of the rest; an unnamed node
    # still gets a line.
    assert listed == [*_FUNCTIONS[:5], "Shape", "Unknown"]
    assert "      ... and 2 more" in messages
    assert "Found 2 'Class' nodes." in messages
    assert messages[-1] == "Analysis complete!"


def test_an_export_without_metadata_still_analyses(
    tmp_path: Path, messages: list[str]
) -> None:
    example.main(_export(tmp_path, metadata=False))
    assert "   Total nodes: 10" in messages
    assert "   Exported at: unknown" in messages
    assert messages[-1] == "Analysis complete!"


def test_a_label_with_no_nodes_lists_no_examples(
    tmp_path: Path, messages: list[str]
) -> None:
    example.log_example_nodes(
        example.load_graph(str(_export(tmp_path))), "Interface", limit=2
    )
    assert messages[-1] == "Found 0 'Interface' nodes."
    assert not any(m.startswith("   Example") for m in messages)


def test_the_command_takes_the_file_as_its_argument(
    tmp_path: Path, messages: list[str]
) -> None:
    result = CliRunner().invoke(_cli(), [str(_export(tmp_path))])
    assert result.exit_code == 0, result.output
    assert messages[-1] == "Analysis complete!"


def test_a_missing_file_exits_with_one_line(
    tmp_path: Path, messages: list[str]
) -> None:
    missing = tmp_path / "nope.json"
    result = CliRunner().invoke(_cli(), [str(missing)])
    assert result.exit_code == 1
    assert messages == [f"Graph file not found: {missing}"]


def test_a_file_that_is_not_an_export_exits_with_the_reason(
    tmp_path: Path, messages: list[str]
) -> None:
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    result = CliRunner().invoke(_cli(), [str(path)])
    assert result.exit_code == 1
    # Reported as one error line, not a traceback.
    assert result.exception is None or isinstance(result.exception, SystemExit)
    errors = [m for m in messages if m.startswith("Error analyzing graph: ")]
    assert len(errors) == 1
    assert "list.json" in errors[0]
