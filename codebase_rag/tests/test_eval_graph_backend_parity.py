"""The backend parity eval's own logic (evals/graph_backend_parity.py).

What decides a verdict is tested here: how rows from two engines are made
comparable and when they count as the same. The end-to-end test dumps a
small embedded graph, replays it through the ingestor into a fresh one and
checks every shipped read query agrees with itself across the two.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import JsonValue
from evals import graph_backend_parity as parity


class _MgNode:
    """The attribute shape of an mgclient Node."""

    def __init__(self, labels: set[str], properties: dict) -> None:
        self.labels, self.properties = labels, properties


def _result(columns: list[str], rows: list[list[JsonValue]]) -> dict[str, JsonValue]:
    return {"columns": columns, "rows": rows}


def test_a_driver_node_and_an_embedded_node_compare_equal() -> None:
    driver = _MgNode({"Function"}, {"name": "f", "score": 0.1234567891})
    embedded = {
        "id": 7,
        "labels": ["Function"],
        "properties": {"name": "f", "score": 0.123456789},
    }
    assert parity._comparable(driver) == parity._comparable(embedded)


def test_collected_lists_compare_as_bags() -> None:
    assert parity._comparable([3, 1, 2]) == parity._comparable([1, 2, 3])


def test_rows_compare_as_a_multiset_with_ids_masked() -> None:
    a = _result(["node_id", "name"], [[1, "a"], [2, "b"]])
    b = _result(["name", "node_id"], [["b", 90], ["a", 91]])
    assert parity.same_rows("MATCH (n) RETURN id(n) AS node_id, n.name AS name", a, b)


def test_a_missing_row_is_a_difference() -> None:
    a = _result(["name"], [["a"], ["a"]])
    b = _result(["name"], [["a"]])
    assert not parity.same_rows("MATCH (n) RETURN n.name AS name", a, b)


def test_limit_without_order_compares_only_the_count() -> None:
    a = _result(["name"], [["a"]])
    b = _result(["name"], [["b"]])
    assert parity.same_rows("MATCH (n) RETURN n.name AS name LIMIT 1", a, b)
    assert not parity.same_rows(
        "MATCH (n) RETURN n.name AS name ORDER BY name LIMIT 1", a, b
    )


@pytest.fixture
def graph(tmp_path: Path) -> str:
    path = str(tmp_path / "source.sqlite")
    with MemgraphIngestor(
        host="unused", port=0, dialect=get_dialect(DIALECT_SQLITE), sqlite_path=path
    ) as ingestor:
        ingestor.ensure_constraints()
        ingestor.ensure_node_batch("Project", {"name": "p", "root_path": "/r"})
        for name in ("a", "b"):
            ingestor.ensure_node_batch(
                "Function",
                {"qualified_name": f"p.m.{name}", "name": name, "start_line": 1},
            )
        ingestor.ensure_relationship_batch(
            ("Function", "qualified_name", "p.m.a"),
            "CALLS",
            ("Function", "qualified_name", "p.m.b"),
        )
    return path


def test_a_replayed_graph_answers_every_query_the_same(graph, tmp_path) -> None:
    runner = CliRunner()
    dump = tmp_path / "graph.json"
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    steps = [
        [
            "dump",
            str(dump),
            "--project",
            "p",
            "--engine",
            "sqlite",
            "--sqlite-path",
            graph,
        ],
        ["run", str(dump), str(first)],
        ["run", str(dump), str(second)],
        ["compare", str(first), str(second)],
    ]
    for args in steps:
        outcome = runner.invoke(parity.app, args)
        assert outcome.exit_code == 0, (args, outcome.output, outcome.exception)
