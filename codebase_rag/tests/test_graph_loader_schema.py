"""Issue #2446: `cgr graph-loader` names what is wrong with a file that is not
a graph export, and a graph without `metadata` still loads.

A missing key surfaced as its bare `KeyError` repr (`Failed to load graph:
'nodes'`), a JSON list as "list indices must be integers or slices, not str",
and a file with valid `nodes` and `relationships` but no informational
`metadata` block was rejected with `'metadata'`.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from codebase_rag.cli import app
from codebase_rag.graph_loader import GraphFileFormatError, load_graph

runner = CliRunner()

_NODE = {"node_id": 1, "labels": ["Function"], "properties": {"name": "f"}}
_NODE_2 = {"node_id": 2, "labels": ["Module"], "properties": {"name": "m"}}
_REL = {"from_id": 2, "to_id": 1, "type": "DEFINES", "properties": {}}
_METADATA = {
    "total_nodes": 2,
    "total_relationships": 1,
    "exported_at": "2026-09-29T00:00:00+00:00",
}


def _write(tmp_path: Path, payload: object, name: str = "graph.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "missing: nodes, relationships"),
        ({"nodes": []}, "missing: relationships"),
        ([1, 2, 3], "expected a JSON object"),
        ({"nodes": {}, "relationships": []}, '"nodes" is not an array'),
        ({"nodes": [{"labels": [], "properties": {}}], "relationships": []}, "node 0"),
        (
            {"nodes": [_NODE], "relationships": [{"to_id": 1, "type": "X"}]},
            "relationship 0",
        ),
        ({"nodes": ["x"], "relationships": []}, "node 0"),
    ],
    ids=[
        "empty-object",
        "no-relationships",
        "a-list",
        "nodes-not-an-array",
        "node-without-id",
        "relationship-without-from",
        "node-not-an-object",
    ],
)
def test_a_file_that_is_not_an_export_says_why(
    tmp_path: Path, payload: object, reason: str
) -> None:
    path = _write(tmp_path, payload)

    with pytest.raises(GraphFileFormatError) as raised:
        load_graph(str(path))

    message = str(raised.value)
    assert "is not a cgr graph export" in message
    assert path.name in message
    assert reason in message


def test_a_graph_without_metadata_loads(tmp_path: Path) -> None:
    path = _write(tmp_path, {"nodes": [_NODE, _NODE_2], "relationships": [_REL]})

    summary = load_graph(str(path)).summary()

    assert summary["total_nodes"] == 2
    assert summary["total_relationships"] == 1
    assert summary["metadata"]["exported_at"] == "unknown"
    assert summary["metadata"]["total_nodes"] == 2


def test_the_cli_prints_the_reason_once(tmp_path: Path) -> None:
    path = _write(tmp_path, {}, "not-a-graph.json")

    result = runner.invoke(app, ["graph-loader", str(path)])

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 1, output
    assert output.count("is not a cgr graph export") == 1, output
    assert "'nodes'" not in output


def test_the_cli_summarises_a_graph_without_metadata(tmp_path: Path) -> None:
    path = _write(tmp_path, {"nodes": [_NODE], "relationships": []})

    result = runner.invoke(app, ["graph-loader", str(path)])

    assert result.exit_code == 0, result.output
    assert "Exported at: unknown" in click.unstyle(result.output)


def test_a_full_export_loads_unchanged(tmp_path: Path) -> None:
    # Negative.
    path = _write(
        tmp_path,
        {"nodes": [_NODE, _NODE_2], "relationships": [_REL], "metadata": _METADATA},
    )

    graph = load_graph(str(path))

    assert graph.metadata == _METADATA
    assert [n.node_id for n in graph.find_nodes_by_label("Function")] == [1]
    assert graph.get_outgoing_relationships(2)[0].type == "DEFINES"


def test_a_missing_file_and_invalid_json_keep_their_messages(tmp_path: Path) -> None:
    # Negative: these were already reported sensibly.
    with pytest.raises(FileNotFoundError, match="Graph file not found"):
        load_graph(str(tmp_path / "absent.json"))
    broken = tmp_path / "broken.json"
    broken.write_text("{\n", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        load_graph(str(broken))
