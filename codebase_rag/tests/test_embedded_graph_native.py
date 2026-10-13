"""The Rust extension's accelerators against the engine paths they replace.

rust/cgr_graph_rs runs the ported procedures (embedded_graph/native.py) at
the calling statement's snapshot and within its deadline. They must give
exactly what the engine gives without them, so these tests run both on the
same input and compare the rows, values with their types. The graph version
counter and the fallbacks belong to the engine and are tested without the
extension too.
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

from codebase_rag.constants import (
    EG_META_GRAPH_VERSION,
    EG_RUST_MODULE,
    EG_RUST_READER,
    EG_SQL_SEED_GRAPH_VERSION,
)
from codebase_rag.embedded_graph import native
from codebase_rag.embedded_graph import sql as sq
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.embedded_graph.limits import (
    Deadline,
    QueryTimeout,
    running_until,
    seconds_left,
)
from codebase_rag.embedded_graph.procedures import Record
from codebase_rag.embedded_graph.types import SqlValue
from codebase_rag.types_defs import (
    ResultValue,
)

RUST = rust_extension()
# A build from before these accelerators has none of them.
needs_rust = pytest.mark.skipif(
    RUST is None or not hasattr(RUST, EG_RUST_READER),
    reason="cgr_graph_rs with the accelerators is not installed",
)

type Rows = list[tuple[ResultValue, ...]]
type Compared = ResultValue | tuple[ResultValue, ...] | Rows


def _same(a: Compared, b: Compared) -> bool:
    """Equal, and of the same types all the way down (1 is not 1.0 or True)."""
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a):
            return math.isnan(b)
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _version(path: Path) -> str | None:
    with closing(sqlite3.connect(path)) as raw:
        row = raw.execute(sq.GRAPH_VERSION).fetchone()
    return row[0] if row else None


# ------------------------------------------------------------ graph version


class TestGraphVersion:
    def test_a_new_graph_starts_at_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "g.sqlite"
        EmbeddedConnection(str(path)).close()
        assert _version(path) == "0"

    def test_each_write_bumps_it_once_and_a_read_never(self, tmp_path: Path) -> None:
        path = tmp_path / "g.sqlite"
        with EmbeddedConnection(str(path)) as conn:
            conn.run("CREATE (:T {k: 1})-[:R]->(:T {k: 2})")
            conn.run("UNWIND [3, 4] AS k MERGE (:T {k: k})")
            conn.run("MATCH (n:T) RETURN count(n) AS c")
            conn.run("CALL pagerank.get() YIELD node RETURN count(node) AS c")
        assert _version(path) == "2"

    def test_the_seed_names_the_row_the_engine_reads_and_bumps(self) -> None:
        name = f"'{EG_META_GRAPH_VERSION}'"
        assert name in EG_SQL_SEED_GRAPH_VERSION
        assert name in sq.GRAPH_VERSION
        assert name in sq.BUMP_GRAPH_VERSION

    def test_an_older_file_gains_it_on_its_first_writable_open(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "g.sqlite"
        EmbeddedConnection(str(path)).close()
        with closing(sqlite3.connect(path)) as raw:
            raw.execute("DELETE FROM cgr_meta WHERE name = ?", (EG_META_GRAPH_VERSION,))
            raw.commit()
        assert _version(path) is None
        EmbeddedConnection(str(path)).close()
        assert _version(path) == "0"

    def test_seconds_left_follows_the_running_deadline(self) -> None:
        assert seconds_left() is None
        with running_until(Deadline.after(60)):
            left = seconds_left()
            assert left is not None and 0 < left <= 60


# ------------------------------------------------------------------ graphs


def _build_graph(path: Path, seed: int, nodes: int, edges: int) -> None:
    rng = random.Random(seed)
    triples = [
        (rng.randrange(nodes), rng.randrange(nodes), rng.choice("ABC"))
        for _ in range(edges)
    ]
    # Self-loops and parallel edges on purpose, in interleaved id order.
    triples += [(0, 0, "A"), (1, 2, "A"), (1, 2, "A"), (2, 1, "B"), (3, 3, "C")]
    rng.shuffle(triples)
    with EmbeddedConnection(str(path)) as conn:
        conn.run("UNWIND range(0, $n - 1) AS i CREATE (:N {k: i})", {"n": nodes})
        for src, dst, rel in triples:
            conn.run(
                f"MATCH (a:N {{k: $a}}), (b:N {{k: $b}}) CREATE (a)-[:{rel}]->(b)",
                {"a": src, "b": dst},
            )


@pytest.fixture(scope="module")
def graphs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("graphs")
    files = {"dense": (1, 14, 30), "other": (2, 14, 30), "small": (3, 6, 8)}
    out = {}
    for name, (seed, nodes, edges) in files.items():
        out[name] = root / f"{name}.sqlite"
        _build_graph(out[name], seed, nodes, edges)
    return out


# -------------------------------------------------------------- procedures

_PROCEDURES = [
    "CALL pagerank.get() YIELD node, rank RETURN node AS n, rank AS r",
    "CALL degree_centrality.get('in') YIELD node, degree RETURN node AS n, degree AS d",
    "CALL weakly_connected_components.get() YIELD node, component_id RETURN node AS n, component_id AS c",
    "CALL nxalg.strongly_connected_components() YIELD components RETURN components AS c",
    "CALL betweenness_centrality.get() YIELD node, betweenness_centrality RETURN node AS n, betweenness_centrality AS b",
    "MATCH (a:N {k: 2}) CALL graph_util.descendants(a) YIELD descendants RETURN descendants AS d",
    "MATCH (a:N {k: 2}) CALL graph_util.ancestors(a) YIELD ancestors RETURN ancestors AS d",
]


@needs_rust
class TestProcedures:
    @pytest.mark.parametrize("query", _PROCEDURES)
    def test_within_a_deadline_they_return_what_python_returns(
        self,
        graphs: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        query: str,
    ) -> None:
        served: list[bool] = []
        real = native.NativeGraph.run_procedure

        def spy(
            graph: native.NativeGraph, name: str, args: Sequence[SqlValue]
        ) -> list[Record] | None:
            records = real(graph, name, args)
            served.append(records is not None)
            return records

        monkeypatch.setattr(native.NativeGraph, "run_procedure", spy)
        with EmbeddedConnection(str(graphs["dense"]), read_only=True) as conn:
            rust = conn.run(query, timeout_s=600).rows
            monkeypatch.setattr(native, "PROCEDURES", False)
            python = conn.run(query, timeout_s=600).rows
        assert served[0] is True
        assert _same(rust, python)

    @pytest.mark.parametrize("query", _PROCEDURES)
    def test_past_the_deadline_they_time_out(
        self,
        graphs: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        query: str,
    ) -> None:
        monkeypatch.setattr(native, "seconds_left", lambda: -1.0)
        with EmbeddedConnection(str(graphs["dense"]), read_only=True) as conn:
            with pytest.raises(QueryTimeout):
                conn.run(query)

    def test_the_standalone_function_keeps_its_deadline_too(
        self, graphs: dict[str, Path]
    ) -> None:
        assert RUST is not None
        path = str(graphs["dense"])
        unlimited = RUST.run_procedure(path, "pagerank.get", [])
        assert RUST.run_procedure(path, "pagerank.get", [], 600.0) == unlimited
        with pytest.raises(TimeoutError):
            RUST.run_procedure(path, "betweenness_centrality.get", [], -1.0)

    def test_one_that_meets_a_newer_graph_answers_from_its_statements(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The review's case: MATCH reads the old graph, another process
        # commits, the CALL must not see the edge the MATCH could not.
        path = str(tmp_path / "g.sqlite")
        with EmbeddedConnection(path) as conn:
            conn.run("CREATE (:T {k: 'a', v: 1})-[:R]->(:T {k: 'b', v: 1})")
        query = (
            "MATCH (a:T {k: 'a'}) CALL graph_util.descendants(a) "
            "YIELD descendants RETURN a.v AS v, size(descendants) AS n"
        )
        monkeypatch.setattr(native, "PROCEDURES", False)
        with EmbeddedConnection(path) as conn:
            before = conn.run(query).rows
        monkeypatch.setattr(native, "PROCEDURES", True)
        real = native._snapshot_version
        served: list[bool] = []

        def committing(conn: sqlite3.Connection) -> str | None:
            version = real(conn)
            with EmbeddedConnection(path) as writer:
                writer.run("MATCH (b:T {k: 'b'}) CREATE (b)-[:R]->(:T {k: 'c', v: 2})")
                writer.run("MATCH (n:T) SET n.v = 2")
            return version

        real_run = native.NativeGraph.run_procedure

        def spy(
            graph: native.NativeGraph, name: str, args: Sequence[SqlValue]
        ) -> list[Record] | None:
            records = real_run(graph, name, args)
            served.append(records is not None)
            return records

        monkeypatch.setattr(native, "_snapshot_version", committing)
        monkeypatch.setattr(native.NativeGraph, "run_procedure", spy)
        with EmbeddedConnection(path) as conn:
            assert conn.run(query).rows == before == [(1, 1)]
        assert served == [False]
        monkeypatch.setattr(native, "_snapshot_version", real)
        with EmbeddedConnection(path) as conn:
            assert conn.run(query).rows == [(2, 2)]
        assert served == [False, True]


# --------------------------------------------------------------- lifecycle


@needs_rust
class TestHandles:
    def test_they_close_after_the_files_last_engine_connection(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "g.sqlite"
        first = EmbeddedConnection(str(path))
        second = EmbeddedConnection(str(path), read_only=True)
        here = str(path.resolve())
        (file,) = [f for f in native._FILES.values() if f.path == here]
        assert file.users == 2
        first.close()
        assert file.users == 1
        assert file in native._FILES.values()
        second.close()
        assert file not in native._FILES.values()

    def test_an_in_memory_graph_has_none(self) -> None:
        with EmbeddedConnection(":memory:") as conn:
            assert conn.native is None


class TestWithoutTheExtension:
    def test_nothing_attaches_and_the_engine_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native, "rust_extension", lambda: None)
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            assert conn.native is None
            conn.run("CREATE (:T {k: 1, f: true})")
            assert conn.run("MATCH (n:T) RETURN n.f AS f, properties(n) AS p").rows == [
                (True, {"k": 1, "f": True})
            ]

    def test_an_older_build_leaves_every_path_to_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            native, "rust_extension", lambda: ModuleType(EG_RUST_MODULE)
        )
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            assert conn.native is not None
            assert conn.native.run_procedure("pagerank.get", []) is None
            conn.run("CREATE (:T {k: 1})-[:R]->(:T {k: 2})")
            assert conn.run("MATCH (a:T)-[:R*1..2]->(b) RETURN b.k AS k").rows == [(2,)]
