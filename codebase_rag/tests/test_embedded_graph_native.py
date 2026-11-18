"""The Rust extension's accelerators against the engine paths they replace.

rust/cgr_graph_rs walks variable-length relationships, writes the ingestor's
node and relationship batches, decodes result rows and runs the ported
procedures (embedded_graph/native.py). Each must give exactly what the engine
gives without it, so these tests run both on the same input and compare:
rows in order, values with their types, files table by table. The graph
version counter and the fallbacks belong to the engine and are tested
without the extension too.
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from codebase_rag.constants import (
    EG_META_GRAPH_VERSION,
    EG_RUST_MODULE,
    EG_RUST_READER,
    EG_SQL_SEED_GRAPH_VERSION,
    MERGE_KEY_PROPS_BY_REL,
    NODE_UNIQUE_CONSTRAINTS,
    EgDecode,
)
from codebase_rag.embedded_graph import executor, limits, native
from codebase_rag.embedded_graph import sql as sq
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.embedded_graph.executor import compile_read, decode
from codebase_rag.embedded_graph.extension import rust_extension
from codebase_rag.embedded_graph.limits import (
    Deadline,
    QueryTimeout,
    running_until,
    seconds_left,
)
from codebase_rag.embedded_graph.procedures import Record
from codebase_rag.embedded_graph.types import SqlValue
from codebase_rag.graph_dialects import DIALECT_SQLITE, get_dialect
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tests.test_embedded_graph_memgraph_parity import MEMGRAPH_PROBES
from codebase_rag.types_defs import (
    JsonValue,
    NodeBatchRow,
    PropertyValue,
    RelBatchRow,
    ResultValue,
)

RUST = rust_extension()
# A build from before these accelerators has none of them.
needs_rust = pytest.mark.skipif(
    RUST is None or not hasattr(RUST, EG_RUST_READER),
    reason="cgr_graph_rs with the accelerators is not installed",
)

type SqlRows = list[tuple[SqlValue, ...]]
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


def _table_dump(path: Path) -> list[list[tuple[str | int | None, ...]]]:
    with closing(sqlite3.connect(path)) as raw:
        return [
            raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (sq.NODES, sq.EDGES, "cgr_meta")
        ]


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

    def test_seconds_left_follows_the_running_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # On a frozen clock: Windows' monotonic clock on Python 3.12 ticks
        # every 15.6 ms, so both reads often agree, and a few minutes after
        # boot `now + 60` crosses 256 and rounds up, a hair past 60.
        now = [241.826]
        monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=lambda: now[0]))
        assert seconds_left() is None
        with running_until(Deadline.after(60)):
            assert seconds_left() == pytest.approx(60)
            now[0] += 45
            assert seconds_left() == pytest.approx(15)


# ------------------------------------------------------------------ values

_CHARS = 'aZ09 "\\/\n\t\r\b\f\x00\x1f\x7fé€\U0001f600 '
_FLOATS = [0.0, -0.0, 0.1, 1.5, 1e-7, 1e16, 1e300, 5e-324, -2.5e-5, 1.0]


def _random_text(rng: random.Random) -> str:
    return "".join(rng.choice(_CHARS) for _ in range(rng.randrange(8)))


def _random_value(rng: random.Random, depth: int = 0) -> JsonValue:
    pick = rng.randrange(10 if depth < 4 else 6)
    if pick == 0:
        return None
    if pick == 1:
        return rng.random() < 0.5
    if pick == 2:
        return rng.randrange(-1000, 1000)
    if pick == 3:
        return rng.choice([2**63 - 1, -(2**63), 2**63, 10**25, -(10**30)])
    if pick == 4:
        return rng.choice(_FLOATS) if rng.random() < 0.6 else rng.uniform(-1e6, 1e6)
    if pick == 5:
        return _random_text(rng)
    if pick < 8:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(4))]
    return {
        _random_text(rng): _random_value(rng, depth + 1)
        for _ in range(rng.randrange(4))
    }


# ---------------------------------------------------------------- decoding

# Text json.loads judges itself: errors, Python's extras, edge numbers.
_HANDMADE = [
    "",
    " ",
    "{",
    "[1,]",
    "[1 2]",
    "01",
    "1.",
    ".5",
    "-",
    "1e",
    "1e+",
    "1.e5",
    "nul",
    "tru",
    "'x'",
    "﻿[]",
    '"\\ud800"',
    '"\\udc00x"',
    '"\\ud83d\\ude00"',
    '"\\ud83dx"',
    '"a\nb"',
    '"\\x41"',
    '"\\u12G4"',
    '"\\u+123"',
    " [1, 2] ",
    '{"a":1,"a":2,"b":3}',
    "NaN",
    "-Infinity",
    "Infinity",
    "-0",
    "-0.0",
    "1E+2",
    "1e400",
    "-1e400",
    "123456789012345678901234567890",
    "9223372036854775807",
    "9223372036854775808",
    "-9223372036854775808",
    "-9223372036854775809",
    "[" * 300 + "]" * 300,
    '{"k": ' * 300 + "1" + "}" * 300,
    '"\\/"',
    '{"":""}',
    "[{}]",
    "1 2",
    "true false",
    '{"a" 1}',
    '{"a":1,}',
    "{1:2}",
    '"tab\tin"',
    "\t[\r\n1\n]\t",
]


def _random_json(rng: random.Random) -> str:
    value = _random_value(rng)
    return json.dumps(
        value,
        ensure_ascii=rng.random() < 0.5,
        indent=rng.choice([None, None, 0, 2]),
        separators=rng.choice([None, (",", ":")]),
    )


def _decoded(
    kinds: list[EgDecode], rows: SqlRows, accelerated: bool
) -> tuple[str, Rows | str]:
    saved, native.DECODE = native.DECODE, accelerated
    try:
        return "rows", native.decode_rows(kinds, rows, decode)
    except Exception as error:
        # Which error, and its message, is the result to compare.
        return "error", f"{type(error).__name__}: {error}"
    finally:
        native.DECODE = saved


def _assert_decoders_agree(kinds: list[EgDecode], rows: SqlRows) -> None:
    python, rust = _decoded(kinds, rows, False), _decoded(kinds, rows, True)
    assert python[0] == rust[0], (rows, python, rust)
    if python[0] == "error":
        assert python == rust, rows
    else:
        assert _same(python[1], rust[1]), (rows, python, rust)


@needs_rust
class TestDecoding:
    @pytest.mark.parametrize("seed", range(4))
    def test_random_json_decodes_as_json_loads_does(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(500):
            _assert_decoders_agree([EgDecode.JSON], [(_random_json(rng),)])

    @pytest.mark.parametrize("text", _HANDMADE)
    @pytest.mark.parametrize("kind", list(EgDecode))
    def test_edge_cases_and_errors_match(self, text: str, kind: EgDecode) -> None:
        _assert_decoders_agree([kind], [(text,)])

    @pytest.mark.parametrize("kind", list(EgDecode))
    def test_values_that_are_not_text_match(self, kind: EgDecode) -> None:
        for value in (None, 0, 1, -7, 2.5, 0.0, "", "x", "1"):
            _assert_decoders_agree([kind], [(value,)])

    def test_a_row_longer_than_its_kinds_is_cut_as_zip_cuts_it(self) -> None:
        rows: SqlRows = [(1, '{"a": 1}', 0, "extra"), (None, "[1, 2.0]", 3, None)]
        _assert_decoders_agree([EgDecode.RAW, EgDecode.JSON, EgDecode.BOOL], rows)
        _assert_decoders_agree([], rows)

    def test_the_engine_corpus_decodes_the_same(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Every result the queries below produce, through both decoders:
        # the Memgraph probes and a graph's nodes, relationships and maps.
        checked: list[int] = []
        real = native.decode_rows

        def both(
            kinds: list[EgDecode],
            rows: SqlRows,
            dec: Callable[[EgDecode, SqlValue], ResultValue],
        ) -> Rows:
            _assert_decoders_agree(kinds, rows)
            checked.append(len(rows))
            return real(kinds, rows, dec)

        monkeypatch.setattr(executor, "decode_rows", both)
        with EmbeddedConnection(":memory:") as conn:
            for query, _, _ in MEMGRAPH_PROBES:
                conn.run(query)
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            conn.run(
                "CREATE (a:Module {qualified_name: 'p.m', name: 'm', "
                "tags: ['x', 'y'], doc: 'line\\nnext é'})"
                "-[:DEFINES {line: 3, weight: 0.5, ok: true}]->"
                "(f:Function {qualified_name: 'p.m.f', start_line: 1, big: 9007199254740993})"
            )
            for query in (
                "MATCH (n) RETURN n",
                "MATCH ()-[r]->() RETURN r",
                "MATCH (n) RETURN properties(n) AS p, labels(n) AS l, keys(n) AS k",
                "MATCH (n) RETURN collect(n) AS ns",
                "MATCH (a)-[r]->(b) RETURN a, r, b, r.ok AS ok, r.weight AS w",
                "MATCH (n) RETURN n.qualified_name AS q UNION ALL RETURN 1 AS q",
                "MATCH (n) WHERE n.nope IS NULL RETURN n.nope AS x, null AS y",
            ):
                conn.run(query)
        assert sum(checked) > 0


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


# ------------------------------------------------------------------- walks


@pytest.fixture
def walking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "WALKS", True)


def _walk_rows(conn: EmbeddedConnection, query: str, accelerated: bool) -> Rows:
    """The rows of the query's first walk CTE, as the statement builds them."""
    plan = compile_read(query, (), True, accelerated)
    name = re.findall(r"\b(eg_vl\d+)\(", plan.sql)[0]
    with_part = plan.sql.rsplit("\n", 1)[0]
    sql = f"{with_part}\nSELECT seed, node, depth, path FROM {name}"
    return conn._sqlite.execute(sql).fetchall()


_ARROWS = ["-[{t}*{h}]->", "<-[{t}*{h}]-", "-[{t}*{h}]-"]
_TYPES = ["", ":A", ":A|B", ":C|A|A"]
_HOPS = ["1..3", "0..2", "2", "..2"]


def _walk_queries(arrow: str) -> list[str]:
    return [
        f"MATCH (a){arrow}(b) RETURN count(*) AS c",
        f"MATCH (a:N {{k: 3}}){arrow}(b) RETURN b.k AS k",
        f"MATCH (b:N) WHERE b.k IN [1, 5] MATCH (a){arrow}(b) RETURN a.k AS k",
        f"MATCH (a:N {{k: 4}}) OPTIONAL MATCH (a){arrow}(b:Nope) RETURN b AS b",
    ]


@needs_rust
@pytest.mark.usefixtures("walking")
class TestWalks:
    @pytest.mark.parametrize("hops", _HOPS)
    @pytest.mark.parametrize("types", _TYPES)
    @pytest.mark.parametrize("arrow", _ARROWS)
    @pytest.mark.parametrize("graph", ["dense", "other"])
    def test_walk_rows_are_the_ctes_rows_in_order(
        self, graphs: dict[str, Path], graph: str, arrow: str, types: str, hops: str
    ) -> None:
        with EmbeddedConnection(str(graphs[graph]), read_only=True) as conn:
            for query in _walk_queries(arrow.format(t=types, h=hops)):
                cte = _walk_rows(conn, query, False)
                rust = _walk_rows(conn, query, True)
                assert rust == cte, query

    @pytest.mark.parametrize("arrow", _ARROWS)
    def test_unbounded_walks_match_on_a_small_graph(
        self, graphs: dict[str, Path], arrow: str
    ) -> None:
        with EmbeddedConnection(str(graphs["small"]), read_only=True) as conn:
            for query in _walk_queries(arrow.format(t="", h="")):
                assert _walk_rows(conn, query, True) == _walk_rows(conn, query, False)

    @pytest.mark.parametrize(
        "query",
        [
            "MATCH (a:N {k: 2})-[:A|B*1..3]->(b) RETURN b.k AS k, count(*) AS n ORDER BY k",
            "MATCH (a)<-[*2]-(b) RETURN a.k AS a, b.k AS b ORDER BY a, b",
            "MATCH (a:N)-[r:A]->(b)-[:A|B*1..2]-(c) RETURN a.k AS a, c.k AS c ORDER BY a, c",
            "MATCH (a:N {k: 1})-[*1..2]->(b)-[*1..2]->(c) RETURN c.k AS c ORDER BY c",
            "MATCH (a:N {k: 0}) OPTIONAL MATCH (a)-[:C*1..2]->(b) RETURN b.k AS k ORDER BY k",
        ],
    )
    def test_queries_answer_as_with_the_cte(
        self,
        graphs: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        query: str,
    ) -> None:
        path = str(graphs["dense"])
        with EmbeddedConnection(path, read_only=True) as conn:
            assert conn.native is not None
            assert conn.native.walks
            rust = conn.run(query).rows
        monkeypatch.setattr(native, "WALKS", False)
        with EmbeddedConnection(path, read_only=True) as conn:
            assert conn.native is None or not conn.native.walks
            python = conn.run(query).rows
        assert _same(rust, python)

    def test_a_seed_that_is_not_a_node_keeps_the_cte(self) -> None:
        plan = compile_read(
            "UNWIND $xs AS x MATCH (x)-[:R*1..2]->(y) RETURN y AS y",
            (("xs", executor.value_type([1, "a"])),),
            True,
            True,
        )
        assert "eg_walk" not in plan.sql

    def test_a_walk_with_property_tests_keeps_the_cte(self) -> None:
        # The Rust walk filters edges by type alone.
        plan = compile_read(
            "MATCH (a:N {k: 1})-[:A*1..2 {w: 1}]->(b) RETURN b AS b", (), True, True
        )
        assert "eg_walk" not in plan.sql
        assert "json_extract(e.props" in plan.sql

    def test_a_walk_that_meets_a_newer_graph_answers_from_its_statements(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Another writer commits between the statement's first read and its
        # walk: the walk sees the new edge, the statement's own reads the old
        # properties, a mix that never existed. The walk must decline it.
        path = str(tmp_path / "g.sqlite")
        with EmbeddedConnection(path) as conn:
            conn.run(
                "CREATE (:T {k: 'a', v: 1})-[:R]->(:T {k: 'b', v: 1})-[:R]->(:T {k: 'c', v: 1})"
            )
        query = "MATCH (a:T {k: 'a'}) MATCH (a)-[:R*1..3]->(x) RETURN x.k AS k, x.v AS v ORDER BY k"
        monkeypatch.setattr(native, "WALKS", False)
        with EmbeddedConnection(path) as conn:
            before = conn.run(query).rows
        monkeypatch.setattr(native, "WALKS", True)
        walked: list[int] = []
        real = native._walk_function

        def committing_first(
            graph: native.NativeGraph, reader: native.Reader
        ) -> Callable[..., str]:
            walk = real(graph, reader)

            def wrapped(*args: str | int | None) -> str:
                if not walked:
                    with EmbeddedConnection(path) as writer:
                        writer.run(
                            "MATCH (c:T {k: 'c'}) CREATE (c)-[:R]->(:T {k: 'd', v: 2})"
                        )
                        writer.run("MATCH (n:T) SET n.v = 2")
                walked.append(1)
                return walk(*args)

            return wrapped

        monkeypatch.setattr(native, "_walk_function", committing_first)
        with EmbeddedConnection(path) as conn:
            assert conn.run(query).rows == before
            # Run again in SQL within the statement, not walked once more.
            assert walked == [1]
            assert conn.run(query).rows == [("b", 2), ("c", 2), ("d", 2)]

    def test_a_walk_past_its_deadline_times_the_statement_out(
        self, graphs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native, "seconds_left", lambda: -1.0)
        with EmbeddedConnection(str(graphs["dense"]), read_only=True) as conn:
            with pytest.raises(QueryTimeout):
                conn.run("MATCH (a)-[:A*1..2]->(b) RETURN count(*) AS c")

    def test_the_reader_walks_within_a_deadline_and_not_past_it(
        self, graphs: dict[str, Path]
    ) -> None:
        assert RUST is not None
        reader = RUST.Reader(str(graphs["dense"]), 1000, 1024, sq.GRAPH_VERSION)
        version = _version(graphs["dense"])
        unlimited = reader.walk(None, "[1, 2]", version, "both", 2)
        assert reader.walk(600.0, "[1, 2]", version, "both", 2) == unlimited
        assert reader.walk(None, "[1, 2]", "stale", "both", 2) is None
        with pytest.raises(TimeoutError):
            reader.walk(-1.0, "[1, 2]", version, "both", 2)
        reader.close()

    def test_another_failure_of_a_walk_is_not_swallowed(
        self, graphs: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(
            graph: native.NativeGraph, reader: native.Reader
        ) -> Callable[..., str]:
            def walk(*args: str | int | None) -> str:
                raise ValueError(args)

            return walk

        monkeypatch.setattr(native, "_walk_function", broken)
        with EmbeddedConnection(str(graphs["dense"]), read_only=True) as conn:
            with pytest.raises(sqlite3.OperationalError):
                conn.run("MATCH (a)-[:A*1..2]->(b) RETURN count(*) AS c")


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


# ------------------------------------------------------------------ ingest

_LABELS = ["Function", "Module", "Project", "File", "Class"]
_REL_TYPES = ["CALLS", "FLOWS_TO", "DEFINES", "IMPORTS"]
_PROP_NAMES = ["name", "doc", "n", "tags", "meta"]


def _random_prop(
    rng: random.Random,
) -> PropertyValue | dict[str, JsonValue] | set[int]:
    pick = rng.randrange(14)
    if pick < 4:
        return _random_text(rng).replace("\ud800", "")
    if pick == 4:
        return rng.randrange(-5, 5000)
    if pick == 5:
        return rng.choice([2**63 - 1, 2**64 + 1, -(2**70)])
    if pick == 6:
        return rng.choice([*_FLOATS, math.nan])
    if pick == 7:
        return rng.random() < 0.5
    if pick == 8:
        return [_random_text(rng) for _ in range(rng.randrange(3))]
    if pick == 9:
        return None
    if pick == 10:
        return {"x": None, "y": rng.randrange(3), "z": {"w": None}}
    if pick == 11:
        return {1, 2}
    return _random_text(rng)


def _random_key(rng: random.Random, label: str, end: bool = False) -> str | int | bool:
    # A boolean key is only ever looked up here: the engine stores a node's
    # True as "True" and rewrites it to "1", so a second MERGE of one fails
    # on the unique index whichever path writes it.
    pick = rng.randrange(20)
    if pick == 0:
        return 7
    if pick == 1 and end:
        return True
    return (
        f"{label}.{rng.choice(['a', 'b', 'c', 'd', 'e', 'q"u', 'ü'])}{rng.randrange(6)}"
    )


def _merge_key_value(rng: random.Random) -> PropertyValue:
    return rng.choice([1, 2, 3, None, True, 2.5, "x", "y", 3])


def _replay(
    path: Path, seed: int, accelerated: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int]:
    """Random batches through the ingestor; how many the extension wrote."""
    monkeypatch.setattr(native, "INGEST", accelerated)
    rng = random.Random(seed)
    written = declined = 0
    real_nodes = native.NativeGraph.merge_nodes
    real_rels = native.NativeGraph.merge_relationships

    def count(done: bool) -> None:
        nonlocal written, declined
        written, declined = written + done, declined + (not done)

    def nodes(
        graph: native.NativeGraph,
        label: str,
        key: str,
        rows: Sequence[NodeBatchRow],
    ) -> bool:
        done = real_nodes(graph, label, key, rows)
        count(done)
        return done

    def rels(
        graph: native.NativeGraph,
        pattern: tuple[str, str, str, str, str],
        spec: native.RelMerge,
        rows: Sequence[RelBatchRow],
    ) -> int | None:
        done = real_rels(graph, pattern, spec, rows)
        count(done is not None)
        return done

    monkeypatch.setattr(native.NativeGraph, "merge_nodes", nodes)
    monkeypatch.setattr(native.NativeGraph, "merge_relationships", rels)
    with MemgraphIngestor(
        "unused",
        0,
        batch_size=7,
        dialect=get_dialect(DIALECT_SQLITE),
        sqlite_path=str(path),
    ) as ingestor:
        ingestor.ensure_constraints()
        for _ in range(160):
            if rng.random() < 0.55:
                label = rng.choice(_LABELS)
                key = NODE_UNIQUE_CONSTRAINTS[label]
                props = {key: _random_key(rng, label)}
                for _ in range(rng.randrange(4)):
                    props[rng.choice([n for n in _PROP_NAMES if n != key])] = (
                        _random_prop(rng)
                    )
                ingestor.ensure_node_batch(label, props)
            else:
                rel = rng.choice(_REL_TYPES)
                ends = []
                for _ in range(2):
                    label = rng.choice(_LABELS)
                    value = (
                        None if rng.random() < 0.05 else _random_key(rng, label, True)
                    )
                    ends.append((label, NODE_UNIQUE_CONSTRAINTS[label], value))
                props: dict[str, PropertyValue] = {}
                for name in MERGE_KEY_PROPS_BY_REL.get(rel, ()):
                    if rng.random() < 0.8:
                        props[name] = _merge_key_value(rng)
                if rng.random() < 0.5:
                    props["resolution"] = _random_prop(rng)
                ingestor.ensure_relationship_batch(ends[0], rel, ends[1], props)
            if rng.random() < 0.05:
                ingestor.flush_all()
    return written, declined


@needs_rust
class TestIngest:
    @pytest.mark.parametrize("seed", range(6))
    def test_random_batches_write_the_same_file(
        self, tmp_path: Path, seed: int, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        python, rust = tmp_path / "python.sqlite", tmp_path / "rust.sqlite"
        assert _replay(python, seed, False, monkeypatch)[0] == 0
        written, declined = _replay(rust, seed, True, monkeypatch)
        assert _table_dump(rust) == _table_dump(python)
        # Both paths ran: most batches in Rust, the odd values in Python.
        assert written > declined > 0

    def test_a_failed_write_leaves_the_batch_to_the_engine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Failing:
            def merge_node_rows(self, *args: str | list[str]) -> bool:
                raise RuntimeError(args)

            def close(self) -> None:
                pass

        monkeypatch.setattr(native, "_new_writer", lambda path: Failing())
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            assert conn.native is not None
            rows: list[NodeBatchRow] = [{"id": "p.m", "props": {"name": "m"}}]
            assert not conn.native.merge_nodes("Module", "qualified_name", rows)

    def test_no_write_inside_a_transaction_of_the_engines(self, tmp_path: Path) -> None:
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            assert conn.native is not None
            conn._sqlite.execute("BEGIN")
            rows: list[NodeBatchRow] = [{"id": "p.m", "props": {}}]
            assert not conn.native.merge_nodes("Module", "qualified_name", rows)
            conn._sqlite.execute("ROLLBACK")
            assert conn.native.merge_nodes("Module", "qualified_name", rows)

    @pytest.mark.parametrize("seed", range(3))
    def test_the_renderer_writes_what_json_dumps_writes(self, seed: int) -> None:
        assert RUST is not None
        rng = random.Random(seed)
        for _ in range(400):
            value = _random_value(rng)
            for compact, separators in ((False, None), (True, (",", ":"))):
                rendered = RUST.render_json(value, compact)
                try:
                    expected = json.dumps(value, separators=separators, allow_nan=False)
                except ValueError:
                    expected = None
                big = re.search(r"\d{19,}", expected or "")
                if big is None:
                    assert rendered == expected, value
                else:
                    assert rendered is None or rendered == expected

    @pytest.mark.parametrize(
        "value",
        [float("nan"), float("inf"), {1, 2}, {1: "a"}, "\ud800", 2**64, b"x"],
    )
    def test_the_renderer_declines_what_the_engine_writes_otherwise(
        self, value: float | set[int] | dict[int, str] | str | int | bytes
    ) -> None:
        assert RUST is not None
        assert RUST.render_json([value], True) is None


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

    def test_they_close_before_the_engines_own_sqlite_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The last engine connection's close checkpoints and deletes the WAL,
        # which must not happen while the extension's SQLite has it open.
        conn = EmbeddedConnection(str(tmp_path / "g.sqlite"))
        assert conn.native is not None
        release = native.release
        open_at_release: list[bool] = []

        def watched(graph: native.NativeGraph | None) -> None:
            try:
                conn._sqlite.execute("SELECT 1")
                open_at_release.append(True)
            except sqlite3.ProgrammingError:
                open_at_release.append(False)
            release(graph)

        monkeypatch.setattr(native, "release", watched)
        conn.close()
        assert open_at_release == [True]
        assert conn.native.lock.acquire(blocking=False)

    def test_an_in_memory_graph_has_none(self) -> None:
        with EmbeddedConnection(":memory:") as conn:
            assert conn.native is None


class TestWithoutTheExtension:
    def test_nothing_attaches_and_rows_decode_in_python(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(native, "rust_extension", lambda: None)
        monkeypatch.setattr(native, "_decoder", lambda: None)
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
        monkeypatch.setattr(native, "_decoder", lambda: None)
        monkeypatch.setattr(native, "WALKS", True)
        with EmbeddedConnection(str(tmp_path / "g.sqlite")) as conn:
            assert conn.native is not None
            assert not conn.native.walks
            rows: list[NodeBatchRow] = [{"id": "p.m", "props": {}}]
            assert not conn.native.merge_nodes("Module", "qualified_name", rows)
            assert conn.native.run_procedure("pagerank.get", []) is None
            conn.run("CREATE (:T {k: 1})-[:R]->(:T {k: 2})")
            assert conn.run("MATCH (a:T)-[:R*1..2]->(b) RETURN b.k AS k").rows == [(2,)]
