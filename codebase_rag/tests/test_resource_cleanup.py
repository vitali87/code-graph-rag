"""The resource prune's verdicts, and its work on dense flows (#3820).

The prune used to follow `FLOWS_TO*0..8` from every Resource, one row per
path: on golang/tools with every capture group on, eleven flows of the
dynamic FILE resource into itself took that walk past 11 GB. It now
spreads liveness out from the anchored Resources one breadth-first level at
a time, each Resource once per level. The per-path statement stays here as
the reference: on small graphs both must prune the same Resources.

Work is counted in SQLite VM steps, through the connection's progress
handler, rather than read off a clock (#3798): the steps a statement takes
on one graph are the same on every run.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.resource_cleanup import prune_unanchored_resources

# The statement the prune ran before #3820.
_PER_PATH_PRUNE = (
    "MATCH (r:Resource) "
    "OPTIONAL MATCH (r)-[:FLOWS_TO*0..8]-(m:Resource)-[]-(x) "
    "WHERE NOT x:Resource "
    "WITH r, count(x) AS anchors "
    "WHERE anchors = 0 "
    "DETACH DELETE r"
)
_REMAINING = "MATCH (r:Resource) RETURN r.qualified_name AS qn ORDER BY qn"

_STEPS_PER_TICK = 1_000
# Ten times what the prune takes on the mesh below: 120 ticks, and 240 on one
# with twice the sources. The per-path walk takes 4,000 on a mesh of five
# sources and more than 200,000 on one of ten.
_TICK_BUDGET = 1_200


def _open(path: Path) -> MemgraphIngestor:
    return MemgraphIngestor(
        host="unused",
        port=0,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(path),
    )


@pytest.fixture
def graph(tmp_path: Path) -> Iterator[MemgraphIngestor]:
    with _open(tmp_path / "graph.sqlite") as ingestor:
        yield ingestor


@pytest.fixture
def reference(tmp_path: Path) -> Iterator[MemgraphIngestor]:
    with _open(tmp_path / "reference.sqlite") as ingestor:
        yield ingestor


def _remaining(ingestor: MemgraphIngestor) -> list[str]:
    return [str(row["qn"]) for row in ingestor.fetch_all(_REMAINING)]


@dataclass
class _Budget:
    ticks: int
    used: int = 0

    def tick(self) -> int:
        self.used += 1
        # Nonzero interrupts the statement.
        return int(self.used > self.ticks)


@contextmanager
def _step_budget(ingestor: MemgraphIngestor, ticks: int) -> Iterator[_Budget]:
    conn = ingestor.conn
    assert isinstance(conn, EmbeddedConnection)
    budget = _Budget(ticks)
    conn._sqlite.set_progress_handler(budget.tick, _STEPS_PER_TICK)
    try:
        yield budget
    finally:
        conn._sqlite.set_progress_handler(None, 0)


def _chain(length: int) -> str:
    """`r0` read by code, then `length` flows, alternating in direction."""
    nodes = ", ".join(
        f"(r{i}:Resource {{qualified_name: 'r{i}'}})" for i in range(length + 1)
    )
    flows = ", ".join(
        f"(r{i})-[:FLOWS_TO]->(r{i + 1})"
        if i % 2
        else f"(r{i + 1})-[:FLOWS_TO]->(r{i})"
        for i in range(length)
    )
    return (
        f"CREATE (f:Function {{qualified_name: 'p.main'}}), {nodes} "
        f"CREATE (f)-[:READS_FROM]->(r0), {flows}"
    )


_CASES: list[tuple[str, str, list[str]]] = [
    (
        "anchored",
        "CREATE (f:Function {qualified_name: 'p.main'}), "
        "(r:Resource {qualified_name: 'env'}) "
        "CREATE (f)-[:READS_FROM]->(r)",
        ["env"],
    ),
    (
        "edgeless",
        "CREATE (:Resource {qualified_name: 'stale'})",
        [],
    ),
    (
        "source_through_sink",
        "CREATE (f:Function {qualified_name: 'p.main'}), "
        "(e:Resource {qualified_name: 'env'}), "
        "(s:Resource {qualified_name: 'stdout'}) "
        "CREATE (f)-[:WRITES_TO]->(s), (e)-[:FLOWS_TO]->(s)",
        ["env", "stdout"],
    ),
    (
        "floating_pair",
        "CREATE (e:Resource {qualified_name: 'env'}), "
        "(s:Resource {qualified_name: 'stdout'}) "
        "CREATE (e)-[:FLOWS_TO]->(s)",
        [],
    ),
    (
        "resolves_to_carries_nothing",
        "CREATE (f:Function {qualified_name: 'p.caller'}), "
        "(u:Resource {qualified_name: 'url'}), "
        "(d:Resource {qualified_name: 'dead_ep'}) "
        "CREATE (f)-[:READS_FROM]->(u), (u)-[:RESOLVES_TO]->(d)",
        ["url"],
    ),
    # The walk's cap: eight flows from the anchor still reach it, nine not.
    ("eight_flows", _chain(8), [f"r{i}" for i in range(9)]),
    ("nine_flows", _chain(9), [f"r{i}" for i in range(9)]),
    (
        "flow_into_code",
        "CREATE (f:Function {qualified_name: 'p.main'}), "
        "(a:Resource {qualified_name: 'a'}), (b:Resource {qualified_name: 'b'}) "
        "CREATE (a)-[:FLOWS_TO]->(f), (b)-[:FLOWS_TO]->(a)",
        ["a", "b"],
    ),
    (
        "unanchored_cycle",
        "CREATE (a:Resource {qualified_name: 'a'}), "
        "(b:Resource {qualified_name: 'b'}), (c:Resource {qualified_name: 'c'}) "
        "CREATE (a)-[:FLOWS_TO]->(b), (b)-[:FLOWS_TO]->(c), (c)-[:FLOWS_TO]->(a)",
        [],
    ),
    (
        "anchored_cycle",
        "CREATE (f:Function {qualified_name: 'p.main'}), "
        "(a:Resource {qualified_name: 'a'}), "
        "(b:Resource {qualified_name: 'b'}), (c:Resource {qualified_name: 'c'}) "
        "CREATE (a)-[:FLOWS_TO]->(b), (b)-[:FLOWS_TO]->(c), (c)-[:FLOWS_TO]->(a), "
        "(f)-[:WRITES_TO]->(c)",
        ["a", "b", "c"],
    ),
    (
        "flow_self_loop",
        "CREATE (a:Resource {qualified_name: 'a'}) CREATE (a)-[:FLOWS_TO]->(a)",
        [],
    ),
]


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [pytest.param(cypher, expected, id=name) for name, cypher, expected in _CASES],
)
def test_the_prune_keeps_what_the_per_path_walk_keeps(
    graph: MemgraphIngestor,
    reference: MemgraphIngestor,
    cypher: str,
    expected: list[str],
) -> None:
    graph.execute_write(cypher)
    reference.execute_write(cypher)

    prune_unanchored_resources(graph)
    reference.execute_write(_PER_PATH_PRUNE)

    assert _remaining(graph) == expected
    assert _remaining(reference) == expected


def _seed_random(ingestor: MemgraphIngestor, seed: int) -> None:
    """Nine Resources, flows between them at random, and a few code edges."""
    rng = random.Random(seed)
    resources = [f"r{i}" for i in range(9)]
    code = ["p.f", "p.g"]
    edges = [
        (rng.choice(resources), rng.choice(resources), "FLOWS_TO") for _ in range(11)
    ]
    edges.append((rng.choice(resources), rng.choice(resources), "RESOLVES_TO"))
    for _ in range(2):
        resource, function = rng.choice(resources), rng.choice(code)
        kind = rng.choice(["READS_FROM", "WRITES_TO", "FLOWS_TO"])
        pair = (function, resource) if rng.random() < 0.5 else (resource, function)
        edges.append((*pair, kind))
    ingestor.execute_write(
        "UNWIND $names AS name CREATE (:Resource {qualified_name: name})",
        {"names": resources},
    )
    ingestor.execute_write(
        "UNWIND $names AS name CREATE (:Function {qualified_name: name})",
        {"names": code},
    )
    for kind in ("FLOWS_TO", "RESOLVES_TO", "READS_FROM", "WRITES_TO"):
        ingestor.execute_write(
            "UNWIND $rows AS row "
            "MATCH (a {qualified_name: row.a}), (b {qualified_name: row.b}) "
            f"CREATE (a)-[:{kind}]->(b)",
            {"rows": [{"a": a, "b": b} for a, b, k in edges if k == kind]},
        )


@pytest.mark.parametrize("seed", range(20))
def test_random_small_graphs_prune_alike(
    graph: MemgraphIngestor, reference: MemgraphIngestor, seed: int
) -> None:
    _seed_random(graph, seed)
    _seed_random(reference, seed)

    prune_unanchored_resources(graph)
    reference.execute_write(_PER_PATH_PRUNE)

    assert _remaining(graph) == _remaining(reference)


_SOURCES = 30
_SINKS = 3


def _seed_mesh(ingestor: MemgraphIngestor, prefix: str) -> None:
    """Every source of `prefix` flowing into every sink of it."""
    ingestor.execute_write(
        "UNWIND range(1, $sources) AS i "
        "CREATE (:Resource {qualified_name: $prefix + '.source' + toString(i)})",
        {"sources": _SOURCES, "prefix": prefix},
    )
    ingestor.execute_write(
        "UNWIND range(1, $sinks) AS i "
        "CREATE (:Resource {qualified_name: $prefix + '.sink' + toString(i)})",
        {"sinks": _SINKS, "prefix": prefix},
    )
    ingestor.execute_write(
        "MATCH (a:Resource), (b:Resource) "
        "WHERE a.qualified_name STARTS WITH $prefix + '.source' "
        "AND b.qualified_name STARTS WITH $prefix + '.sink' "
        "CREATE (a)-[:FLOWS_TO]->(b)",
        {"prefix": prefix},
    )


def _seed_self_flows(ingestor: MemgraphIngestor, name: str) -> None:
    """golang/tools' shape: a Resource flowing into itself from eleven sites
    and a source flowing into it. Every order of the eleven is a path."""
    ingestor.execute_write(
        "CREATE (:Resource {qualified_name: $name})"
        "<-[:FLOWS_TO]-(:Resource {qualified_name: $name + '.source'})",
        {"name": name},
    )
    ingestor.execute_write(
        "MATCH (r:Resource {qualified_name: $name}) "
        "UNWIND range(1, 11) AS i CREATE (r)-[:FLOWS_TO]->(r)",
        {"name": name},
    )


def _anchor(ingestor: MemgraphIngestor, name: str) -> None:
    ingestor.execute_write(
        "MATCH (r:Resource {qualified_name: $name}) "
        "CREATE (:Function {qualified_name: 'p.main'})-[:WRITES_TO]->(r)",
        {"name": name},
    )


@pytest.fixture(params=["mesh", "self_flows"])
def dense(request: pytest.FixtureRequest, graph: MemgraphIngestor) -> list[str]:
    """A live and a dead copy of one dense shape: the Resources that stay."""
    if request.param == "mesh":
        _seed_mesh(graph, "live")
        _seed_mesh(graph, "dead")
        _anchor(graph, "live.sink1")
        return sorted(
            [f"live.source{i}" for i in range(1, _SOURCES + 1)]
            + [f"live.sink{i}" for i in range(1, _SINKS + 1)]
        )
    _seed_self_flows(graph, "live")
    _seed_self_flows(graph, "dead")
    _anchor(graph, "live")
    return ["live", "live.source"]


def test_dense_flows_are_pruned_within_the_budget(
    graph: MemgraphIngestor, dense: list[str]
) -> None:
    with _step_budget(graph, _TICK_BUDGET):
        prune_unanchored_resources(graph)

    assert _remaining(graph) == dense


def test_the_per_path_walk_overruns_that_budget(
    graph: MemgraphIngestor, dense: list[str]
) -> None:
    # Negative control: each shape is dense enough to tell the two apart.
    with (
        _step_budget(graph, _TICK_BUDGET),
        pytest.raises(sqlite3.OperationalError, match="interrupted"),
    ):
        graph.execute_write(_PER_PATH_PRUNE)

    assert len(_remaining(graph)) == 2 * len(dense)
