"""The optional Rust procedures against the Python ones they replace.

`rust/cgr_graph_rs` ports the embedded engine's whole-graph algorithms
(issue #386). A port that drifted would change answers only for users who
installed it, so each procedure runs both ways on the same random graphs
and the records must be identical, floats included: the port performs the
same floating-point operations in the same order. Skipped where the
extension is not installed.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from codebase_rag.embedded_graph import procedures
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.embedded_graph.types import SqlValue

rust = pytest.importorskip("cgr_graph_rs")

_PORTED = [
    ("pagerank.get", []),
    ("pagerank.get", [10, 0.5, 1e-3]),
    ("degree_centrality.get", []),
    ("degree_centrality.get", ["in"]),
    ("weakly_connected_components.get", []),
    ("nxalg.strongly_connected_components", []),
    ("betweenness_centrality.get", []),
]


def _random_graph(path: Path, seed: int) -> list[int]:
    rng = random.Random(seed)
    nodes = 60
    edges = [(rng.randrange(nodes), rng.randrange(nodes)) for _ in range(150)]
    with EmbeddedConnection(str(path)) as conn:
        conn.run("UNWIND range(0, $n - 1) AS i CREATE (:N {k: i})", {"n": nodes})
        conn.run(
            "UNWIND $edges AS e MATCH (a:N {k: e[0]}), (b:N {k: e[1]}) "
            "CREATE (a)-[:R]->(b)",
            {"edges": [list(e) for e in edges]},
        )
        # Gaps in the id sequence, as deleted projects leave them.
        conn.run("MATCH (n:N) WHERE n.k % 7 = 3 DETACH DELETE n")
        rows = conn.run("MATCH (n:N) RETURN id(n) AS id ORDER BY id").rows
    return [int(r[0]) for r in rows]


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize(("name", "args"), _PORTED)
def test_ported_procedures_match_python(
    tmp_path: Path, seed: int, name: str, args: list[SqlValue]
) -> None:
    path = tmp_path / "graph.sqlite"
    _random_graph(path, seed)
    with EmbeddedConnection(str(path)) as conn:
        python = list(procedures.PROCEDURES[name].fn(conn._sqlite, args))
    assert rust.run_procedure(str(path), name, args) == python


@pytest.mark.parametrize("seed", [1, 2])
def test_reachability_matches_python(tmp_path: Path, seed: int) -> None:
    path = tmp_path / "graph.sqlite"
    ids = _random_graph(path, seed)
    with EmbeddedConnection(str(path)) as conn:
        for node in ids[:10]:
            for name in ("graph_util.ancestors", "graph_util.descendants"):
                python = list(procedures.PROCEDURES[name].fn(conn._sqlite, [node]))
                assert rust.run_procedure(str(path), name, [node]) == python


def test_unported_procedures_and_memory_graphs_stay_in_python(tmp_path) -> None:
    path = tmp_path / "graph.sqlite"
    _random_graph(path, 1)
    assert rust.run_procedure(str(path), "nxalg.simple_cycles", []) is None
    assert rust.run_procedure("", "pagerank.get", []) is None
