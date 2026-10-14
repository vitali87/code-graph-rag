"""The embedded graph engine: what cgr writes and reads through it (issue #386).

Reads are checked against Memgraph's recorded answers in
test_embedded_graph_memgraph_parity.py; these tests cover the write path the
ingestor drives, the read-only guard, the procedures the prompt advertises,
and the compiler rewrites that keep big intermediate values off every row.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.constants import (
    EG_BYTES_PER_MB,
    EG_PLANNER_STATISTICS,
    EG_RANGE_ITEM_BYTES,
)
from codebase_rag.embedded_graph import cypher_ast as a
from codebase_rag.embedded_graph import functions, limits, procedures
from codebase_rag.embedded_graph.compiler import ALL_LIVE, live_after
from codebase_rag.embedded_graph.connection import EmbeddedConnection
from codebase_rag.embedded_graph.executor import compile_read, value_type
from codebase_rag.embedded_graph.expressions import (
    CompileError,
)
from codebase_rag.embedded_graph.lexer import CypherSyntaxError
from codebase_rag.embedded_graph.limits import Deadline, QueryTimeout, running_until
from codebase_rag.embedded_graph.parser import parse
from codebase_rag.exceptions import ReadOnlyQueryError
from codebase_rag.graph_dialects import (
    DIALECT_SQLITE,
    MemgraphDialect,
    Neo4jDialect,
    get_dialect,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import CypherParamValue, ResultValue

# Pieces the graph audit formats into a query, not queries themselves.
_QUERY_FRAGMENTS = frozenset(
    {"CYPHER_AUDIT_IS_NULL", "CYPHER_AUDIT_MISSING_REQUIRED", "CYPHER_AUDIT_OR"}
)


_QUERY_NAMES = sorted(
    n
    for n in dir(cq)
    if n.startswith("CYPHER_")
    and isinstance(getattr(cq, n), str)
    and n not in _QUERY_FRAGMENTS
)


# Well past the 0.05 s deadline, far short of the unbounded match.
_REGEX_STOP_S = 5.0
# A deadline clock coarser than `regex`'s own, as Windows' monotonic clock
# (15.6 ms) was before Python 3.13; wide enough that the 0.05 s deadline
# falls inside its first tick on any runner.
_COARSE_TICK_S = 0.2


@pytest.fixture
def conn() -> Iterator[EmbeddedConnection]:
    with EmbeddedConnection(":memory:") as c:
        yield c


def _rows(
    conn: EmbeddedConnection, query: str, **params: CypherParamValue
) -> list[tuple[ResultValue, ...]]:
    return conn.run(query, params).rows


class TestParser:
    @pytest.mark.parametrize("name", _QUERY_NAMES)
    def test_every_query_constant_parses(self, name: str) -> None:
        parse(getattr(cq, name))

    @pytest.mark.parametrize(
        "statement",
        [
            MemgraphDialect().create_index("Function", "name"),
            Neo4jDialect().create_index("Function", "name"),
            MemgraphDialect().create_constraint("Module", "qualified_name"),
            Neo4jDialect().create_constraint("Module", "qualified_name"),
        ],
    )
    def test_both_engines_ddl_names_label_and_property(self, statement: str) -> None:
        # The store derives its own schema from the label and property, so
        # whichever engine's spelling arrives must reduce to the same pair.
        command = parse(statement)
        assert isinstance(command, a.SchemaCommand)
        assert command.label in ("Function", "Module")
        assert command.props in (("name",), ("qualified_name",))

    def test_a_syntax_error_names_its_offset(self) -> None:
        with pytest.raises(CypherSyntaxError):
            parse("MATCH (n RETURN n")

    def test_a_syntax_error_quotes_the_punctuation_it_expected(self) -> None:
        # The message goes back to the model for a repair attempt, so it
        # names the `)` itself rather than the enum member that spells it.
        with pytest.raises(CypherSyntaxError) as raised:
            parse("MATCH (n RETURN n")
        assert str(raised.value) == (
            "Cypher syntax error at offset 9: unexpected 'RETURN', expected ')'"
        )


class TestWrites:
    def test_merge_is_idempotent_on_the_unique_key(self, conn) -> None:
        for _ in range(2):
            conn.run(
                "UNWIND $batch AS row MERGE (n:Module {qualified_name: row.qn}) "
                "SET n += row.props",
                {"batch": [{"qn": "p.m", "props": {"name": "m", "path": "m.py"}}]},
            )
        assert _rows(conn, "MATCH (n:Module) RETURN count(n) AS c") == [(1,)]
        assert _rows(conn, "MATCH (n:Module) RETURN n.path AS p") == [("m.py",)]

    def test_relationships_merge_once_between_bound_ends(self, conn) -> None:
        conn.run(
            "CREATE (:Module {qualified_name: 'a'}), (:Module {qualified_name: 'b'})"
        )
        query = (
            "UNWIND $batch AS row MATCH (a:Module {qualified_name: row.f}), "
            "(b:Module {qualified_name: row.t}) MERGE (a)-[r:IMPORTS]->(b) "
            "RETURN count(r) AS created"
        )
        batch = {"batch": [{"f": "a", "t": "b"}]}
        conn.run(query, batch)
        conn.run(query, batch)
        assert _rows(conn, "MATCH ()-[r:IMPORTS]->() RETURN count(r) AS c") == [(1,)]

    def test_on_create_and_on_match_run_on_their_own_rows(self, conn) -> None:
        query = (
            "MERGE (n:T {k: 'z'}) ON CREATE SET n.created = 1 "
            "ON MATCH SET n.matched = 1 RETURN n.created AS c, n.matched AS m"
        )
        assert _rows(conn, query) == [(1, None)]
        assert _rows(conn, query) == [(1, 1)]

    def test_setting_a_key_property_moves_the_unique_key(self, conn) -> None:
        conn.run("CREATE (:Function {qualified_name: 'old'})")
        conn.run("MATCH (n:Function) SET n.qualified_name = 'new'")
        assert _rows(
            conn, "MATCH (n:Function {qualified_name: 'new'}) RETURN count(n) AS c"
        ) == [(1,)]

    def test_delete_refuses_a_node_with_relationships(self, conn) -> None:
        conn.run("CREATE (:T {k: 'a'})-[:R]->(:T {k: 'b'})")
        with pytest.raises(CompileError):
            conn.run("MATCH (n:T {k: 'a'}) DELETE n")
        conn.run("MATCH (n:T {k: 'a'}) DETACH DELETE n")
        assert _rows(conn, "MATCH (n) RETURN count(n) AS c") == [(1,)]
        assert _rows(conn, "MATCH ()-[r]->() RETURN count(r) AS c") == [(0,)]

    def test_a_failed_statement_changes_nothing(self, conn) -> None:
        conn.run("CREATE (:T {k: 'a'})-[:R]->(:T {k: 'b'})")
        with pytest.raises(CompileError):
            conn.run("MATCH (n:T) SET n.touched = true WITH n DELETE n")
        assert _rows(conn, "MATCH (n:T) WHERE n.touched RETURN count(n) AS c") == [(0,)]


class TestReadOnly:
    def test_explain_reports_writes_in_memgraph_operator_names(self, conn) -> None:
        plan = [row[0] for row in _rows(conn, "EXPLAIN MATCH (n) SET n.x = 1")]
        assert " * SetProperties" in plan

    def test_a_read_only_connection_refuses_writes(self, tmp_path: Path) -> None:
        path = str(tmp_path / "g.sqlite")
        with EmbeddedConnection(path) as writer:
            writer.run("CREATE (:T {k: 'a'})")
        with EmbeddedConnection(path, read_only=True) as reader:
            assert reader.run("MATCH (n:T) RETURN n.k AS k").rows == [("a",)]
            with pytest.raises(CompileError):
                reader.run("MATCH (n:T) SET n.k = 'b'")

    def test_procedures_run_on_a_read_only_connection(self, tmp_path: Path) -> None:
        # CALL works through temp tables, which a read-only file still allows.
        path = str(tmp_path / "g.sqlite")
        with EmbeddedConnection(path) as writer:
            writer.run("CREATE (:T {k: 'a'})-[:R]->(:T {k: 'b'})")
        with EmbeddedConnection(path, read_only=True) as reader:
            rows = reader.run(
                "CALL pagerank.get() YIELD node, rank RETURN node.k AS k ORDER BY rank DESC"
            ).rows
        assert rows == [("b",), ("a",)]


class TestLimits:
    """A statement stops at its deadline instead of running on in a worker
    thread after its caller gave up (CodeRabbit on #3377)."""

    _CROSS_JOIN = "MATCH (a:N), (b:N), (c:N) RETURN count(*) AS n"

    @pytest.fixture
    def wide(self, conn) -> EmbeddedConnection:
        conn.run("UNWIND range(1, 400) AS i CREATE (:N {i: i})")
        return conn

    def test_a_long_statement_stops_at_its_deadline(self, wide) -> None:
        with pytest.raises(QueryTimeout):
            wide.run(self._CROSS_JOIN, timeout_s=0.05)

    def test_the_connection_is_usable_after_a_timeout(self, wide) -> None:
        with pytest.raises(QueryTimeout):
            wide.run(self._CROSS_JOIN, timeout_s=0.05)
        # The handler is cleared: a statement without a limit runs to the end.
        assert wide.run("MATCH (a:N) RETURN count(a) AS n").rows == [(400,)]

    def test_a_write_stops_at_its_deadline_and_writes_nothing(self, wide) -> None:
        # SQLite rolls an interrupted write's transaction back itself, so the
        # engine's own rollback must not run again and mask the timeout.
        with pytest.raises(QueryTimeout):
            wide.run("UNWIND range(1, 3000000) AS i CREATE (:W {i: i})", timeout_s=0.05)
        assert wide.run("MATCH (w:W) RETURN count(w) AS n").rows == [(0,)]
        assert wide.run("MATCH (a:N) RETURN count(a) AS n").rows == [(400,)]

    def test_a_backtracking_regex_stops_at_its_deadline(self, conn) -> None:
        # SQLite's progress handler cannot interrupt a function mid-call; the
        # match keeps the deadline itself. Unbounded, this one runs for hours.
        started = time.monotonic()
        with pytest.raises(QueryTimeout):
            conn.run("RETURN $s =~ '(a|aa)+c' AS m", {"s": "a" * 60}, timeout_s=0.05)
        assert time.monotonic() - started < _REGEX_STOP_S
        assert conn.run("RETURN 'abc' =~ 'a.c' AS m").rows == [(True,)]

    def test_a_regex_timeout_is_reported_on_a_coarse_clock(
        self, conn, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `regex` gives up on its own clock while the deadline's still reads
        # inside its first tick; the timeout must not surface as a bare
        # "user-defined function raised exception".
        origin = time.monotonic()

        def coarse() -> float:
            return (time.monotonic() - origin) // _COARSE_TICK_S * _COARSE_TICK_S

        monkeypatch.setattr(limits, "time", SimpleNamespace(monotonic=coarse))
        with pytest.raises(QueryTimeout):
            conn.run("RETURN $s =~ '(a|aa)+c' AS m", {"s": "a" * 60}, timeout_s=0.05)

    def test_a_range_the_memory_limit_cannot_hold_is_refused(self) -> None:
        # Its list is Python's memory, which SQLite's heap limit never sees.
        # Each connection gets its own limit; registering directly keeps the
        # process-wide SQLite heap limit out of this test.
        raw = sqlite3.connect(":memory:")
        functions.register(raw, heap_limit_mb=1)
        items = EG_BYTES_PER_MB // EG_RANGE_ITEM_BYTES
        assert raw.execute(
            "SELECT json_array_length(eg_range(1, ?, 1))", (items,)
        ).fetchone() == (items,)
        # A SELECT's execute() steps to its first row, so the refusal comes
        # from the call itself.
        with pytest.raises(MemoryError):
            raw.execute("SELECT eg_range(1, ?, 1)", (items + 1,))
        with pytest.raises(MemoryError):
            raw.execute("SELECT eg_range(?, ?, 1)", (-(2**63), 2**63 - 2))
        raw.close()

    def test_a_python_procedure_stops_at_its_deadline(self, wide) -> None:
        expired = Deadline(at=0.0)
        with running_until(expired), pytest.raises(QueryTimeout):
            list(
                procedures.PROCEDURES["betweenness_centrality.get"].fn(wide._sqlite, [])
            )

    def test_generated_queries_run_with_the_query_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = str(tmp_path / "g.sqlite")
        with EmbeddedConnection(path) as writer:
            writer.run("UNWIND range(1, 400) AS i CREATE (:N {i: i})")
        monkeypatch.setattr(
            "codebase_rag.services.graph_service.settings.QUERY_TIMEOUT_S", 0.05
        )
        ingestor = MemgraphIngestor(
            "", 0, dialect=get_dialect(DIALECT_SQLITE), sqlite_path=path
        )
        with ingestor, pytest.raises(TimeoutError):
            ingestor.fetch_read_only(self._CROSS_JOIN)


class TestProcedures:
    @pytest.fixture
    def cyclic(self, conn) -> EmbeddedConnection:
        conn.run(
            "CREATE (a:F {k: 'a'})-[:CALLS]->(b:F {k: 'b'})-[:CALLS]->(a), "
            "(b)-[:CALLS]->(c:F {k: 'c'})"
        )
        return conn

    def test_strongly_connected_components(self, cyclic) -> None:
        rows = _rows(
            cyclic,
            "CALL nxalg.strongly_connected_components() YIELD components "
            "UNWIND components AS comp RETURN size(comp) AS n ORDER BY n DESC",
        )
        assert rows == [(2,), (1,)]

    def test_nested_node_lists_come_back_as_nodes(self, cyclic) -> None:
        """Memgraph returns each component as a list of nodes, not ids."""
        rows = _rows(
            cyclic,
            "CALL nxalg.strongly_connected_components() YIELD components "
            "RETURN components",
        )
        [(components,)] = rows
        keys = sorted(
            sorted(node["properties"]["k"] for node in comp) for comp in components
        )
        assert keys == [["a", "b"], ["c"]]

    def test_a_standalone_call_returns_nodes(self, cyclic) -> None:
        result = cyclic.run("CALL pagerank.get()")
        assert result.columns == ["node", "rank"]
        assert sorted(row[0]["properties"]["k"] for row in result.rows) == [
            "a",
            "b",
            "c",
        ]

    def test_shortest_path_returns_its_nodes(self, cyclic) -> None:
        rows = _rows(
            cyclic,
            "MATCH (a:F {k: 'a'}), (c:F {k: 'c'}) CALL nxalg.shortest_path(a, c) "
            "YIELD path RETURN [n IN path | n.k] AS ks",
        )
        assert rows == [(["a", "b", "c"],)]

    def test_ancestors(self, cyclic) -> None:
        rows = _rows(
            cyclic,
            "MATCH (c:F {k: 'c'}) CALL graph_util.ancestors(c) YIELD ancestors "
            "UNWIND ancestors AS n RETURN n.k AS k ORDER BY k",
        )
        assert rows == [("a",), ("b",)]


class TestCompiler:
    def test_a_projection_ends_what_earlier_clauses_must_carry(self) -> None:
        query = parse("MATCH (a)-[:R]->(b) WITH a MATCH (a)-[:S]->(c) RETURN c.k AS k")
        assert isinstance(query, a.Query)
        live = live_after(query.parts[0].clauses)
        assert live[0] == {"a"}
        assert live[2] == {"c"}

    def test_a_star_projection_keeps_everything(self) -> None:
        query = parse("MATCH (a)-[:R]->(b) WITH * RETURN a.k AS k")
        assert isinstance(query, a.Query)
        assert ALL_LIVE in live_after(query.parts[0].clauses)[0]

    def test_a_single_row_list_is_read_once_not_carried(self) -> None:
        # The project export collects every owned node into one list, then
        # matches relationships against it: carried per row, that list cost
        # 8.5 GB on a 38k-node graph.
        plan = compile_read(
            "MATCH (n:Module) WITH collect(n) AS owned "
            "MATCH (a)-[r]->(b) WHERE a IN owned RETURN count(r) AS c",
            (),
            True,
        )
        assert "IN (SELECT" in plan.sql
        assert "GROUP BY" not in plan.sql

    def test_a_walk_starts_only_where_the_where_clause_allows(self) -> None:
        # Seeded from every Module and filtered afterwards, the module walk
        # in endpoint_routes took 7 minutes on a 38k-node graph.
        plan = compile_read(
            "MATCH (m:Module)-[:DEFINES*]->(f) WHERE m.qualified_name IN $qns "
            "RETURN f.name AS name",
            (("qns", value_type(["a"])),),
            True,
        )
        seed = plan.sql.split("UNION ALL")[0]
        assert "json_each" in seed

    def test_a_walk_expands_from_its_queue_row_outwards(self) -> None:
        # CROSS JOIN fixes the join order: the recursive step looks up the
        # edges leaving its one queue row instead of scanning a whole type.
        plan = compile_read("MATCH (a)-[:R*1..3]->(b) RETURN count(*) AS c", (), True)
        assert "CROSS JOIN cgr_edges" in plan.sql

    def test_walks_agree_with_and_without_a_pushed_seed(self, conn) -> None:
        conn.run(
            "CREATE (:Module {qualified_name: 'p.a'})-[:DEFINES]->"
            "(:Function {qualified_name: 'p.a.f', name: 'f'}), "
            "(:Module {qualified_name: 'p.b'})-[:DEFINES]->"
            "(:Function {qualified_name: 'p.b.g', name: 'g'})"
        )
        pushed = _rows(
            conn,
            "MATCH (m:Module)-[:DEFINES*]->(f) WHERE m.qualified_name IN $qns "
            "RETURN f.name AS name",
            qns=["p.a"],
        )
        assert pushed == [("f",)]

    def test_grouping_by_a_constant_over_no_rows_gives_no_rows(self, conn) -> None:
        rows = _rows(
            conn,
            "WITH [1] AS l MATCH (n:Nope) RETURN l AS l, count(n) AS c",
        )
        assert rows == []

    def test_an_aggregate_over_no_rows_gives_one_row(self, conn) -> None:
        assert _rows(conn, "MATCH (n:Nope) RETURN count(n) AS c") == [(0,)]


class TestWalkProperties:
    # A property map on a variable-length relationship tests every edge of
    # the path, as on a single one; it used to be dropped, so the walk took
    # every CALLS edge.
    _GRAPH = (
        "CREATE (a:F {name: 'a'}), (b:F {name: 'b'}), (c:F {name: 'c'}), "
        "(d:F {name: 'd'}), (e:F {name: 'e'}), "
        "(a)-[:CALLS {resolution: 'exact'}]->(b), "
        "(b)-[:CALLS {resolution: 'fuzzy'}]->(c), "
        "(a)-[:CALLS]->(d), "
        "(b)-[:CALLS {resolution: 'exact'}]->(e)"
    )

    def test_every_walked_edge_passes_the_property_tests(self, conn) -> None:
        _rows(conn, self._GRAPH)
        rows = _rows(
            conn,
            "MATCH (a:F {name: 'a'})-[:CALLS*1..2 {resolution: 'exact'}]->(b) "
            "RETURN b.name AS name ORDER BY name",
        )
        assert rows == [("b",), ("e",)]

    def test_a_parameter_can_set_the_tested_value(self, conn) -> None:
        _rows(conn, self._GRAPH)
        rows = _rows(
            conn,
            "MATCH (a:F {name: 'a'})-[:CALLS*1..2 {resolution: $r}]->(b) "
            "RETURN count(*) AS n",
            r="fuzzy",
        )
        assert rows == [(0,)]

    def test_a_value_read_from_the_row_is_refused(self, conn) -> None:
        _rows(conn, self._GRAPH)
        with pytest.raises(CompileError):
            _rows(
                conn,
                "MATCH (a:F) MATCH (a)-[:CALLS*1..2 {resolution: a.name}]->(b) "
                "RETURN b AS b",
            )


_SCHEMA_1_NODES = (
    "CREATE TABLE cgr_nodes(id INTEGER PRIMARY KEY, label TEXT NOT NULL, "
    "key TEXT, props TEXT NOT NULL DEFAULT '{}');"
    "CREATE UNIQUE INDEX cgr_nodes_label_key ON cgr_nodes(label, key) "
    "WHERE key IS NOT NULL;"
    "CREATE INDEX cgr_nodes_prop_name ON cgr_nodes(label, json_extract(props, '$.name'));"
    "INSERT INTO cgr_nodes(id, label, key, props) VALUES "
    "(1, 'Function', 'm.a', '{\"name\": \"a\"}'), "
    "(2, 'Function', 'm.b', '{\"name\": \"b\"}');"
)


class TestNodeIds:
    # Vector stores key embeddings by node id, and a sync without embeddings
    # leaves a deleted function's vector behind. A reused id would make that
    # vector find whichever node took the id.
    def test_a_deleted_node_id_is_never_handed_out_again(self, conn) -> None:
        _rows(conn, "CREATE (:Function {qualified_name: 'm.a'})")
        _rows(conn, "CREATE (:Function {qualified_name: 'm.b'})")
        ((old,),) = _rows(
            conn, "MATCH (f:Function {qualified_name: 'm.b'}) RETURN id(f) AS id"
        )
        _rows(conn, "MATCH (f:Function {qualified_name: 'm.b'}) DETACH DELETE f")
        ((new,),) = _rows(
            conn, "CREATE (f:Function {qualified_name: 'm.c'}) RETURN id(f) AS id"
        )
        assert isinstance(old, int)
        assert isinstance(new, int)
        assert new > old

    def test_an_older_graph_file_stops_reusing_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "g.sqlite"
        with sqlite3.connect(path) as raw:
            raw.executescript(_SCHEMA_1_NODES)
        with EmbeddedConnection(str(path)) as conn:
            kept = _rows(
                conn,
                "MATCH (f:Function) RETURN id(f) AS id, f.name AS name ORDER BY id",
            )
            _rows(conn, "MATCH (f:Function {name: 'b'}) DETACH DELETE f")
            ((new,),) = _rows(
                conn, "CREATE (f:Function {name: 'c'}) RETURN id(f) AS id"
            )
        assert kept == [(1, "a"), (2, "b")]
        assert new == 3
        with sqlite3.connect(path) as raw:
            indexes = {
                name
                for (name,) in raw.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'cgr_nodes'"
                )
            }
            (ddl,) = raw.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'cgr_nodes'"
            ).fetchone()
        assert "AUTOINCREMENT" in ddl
        assert {"cgr_nodes_label_key", "cgr_nodes_prop_name"} <= indexes


class TestStatistics:
    def test_an_analyze_of_a_small_graph_does_not_stick(self, tmp_path: Path) -> None:
        # Measured while a sync was still writing, statistics described a
        # near-empty graph and every relationship batch was planned as if
        # each label held one node.
        path = tmp_path / "g.sqlite"
        with EmbeddedConnection(str(path)) as conn:
            conn.run("CREATE (:T {k: 1})-[:R]->(:T {k: 2})")
        with sqlite3.connect(path) as raw:
            raw.execute("ANALYZE")
        with EmbeddedConnection(str(path)):
            pass
        with sqlite3.connect(path) as raw:
            stats = set(
                raw.execute(
                    "SELECT tbl, idx, stat FROM sqlite_stat1 "
                    "WHERE tbl IN ('cgr_nodes', 'cgr_edges')"
                )
            )
        assert stats == set(EG_PLANNER_STATISTICS)

    def test_a_batch_looks_each_row_up_by_its_unique_key(self, conn) -> None:
        # The ingestor's relationship batches: each row's two endpoints
        # must be index seeks on (label, key), driven by the batch.
        plan = compile_read(
            "UNWIND $batch AS row MATCH (a:Method {qualified_name: row.from_val}), "
            "(b:Method {qualified_name: row.to_val}) RETURN a.name AS name",
            (("batch", value_type([{"from_val": "x", "to_val": "y"}])),),
            True,
        )
        binds = {f":{p.placeholder}": "[]" for p in plan.params}
        steps = [
            row[3]
            for row in conn._sqlite.execute(
                "EXPLAIN QUERY PLAN " + plan.sql,
                {k.lstrip(":"): v for k, v in binds.items()},
            )
        ]
        # Reading `a.name` afterwards is a rowid lookup; the two pattern
        # nodes are the steps that search an index of cgr_nodes.
        index_steps = [step for step in steps if "INDEX cgr_nodes" in step]
        assert len(index_steps) == 2, steps
        assert all("label=? AND key=?" in step for step in index_steps), steps


class TestIngestor:
    @pytest.fixture
    def ingestor(self, tmp_path: Path) -> Iterator[MemgraphIngestor]:
        with MemgraphIngestor(
            host="unused",
            port=0,
            dialect=get_dialect(DIALECT_SQLITE),
            sqlite_path=str(tmp_path / "graph.sqlite"),
        ) as ingestor:
            ingestor.ensure_constraints()
            yield ingestor

    def test_nodes_and_relationships_flush_and_read_back(self, ingestor) -> None:
        ingestor.ensure_node_batch("Project", {"name": "p"})
        ingestor.ensure_node_batch(
            "Module", {"qualified_name": "p.m", "name": "m", "path": "m.py"}
        )
        ingestor.ensure_relationship_batch(
            ("Project", "name", "p"),
            "CONTAINS_MODULE",
            ("Module", "qualified_name", "p.m"),
        )
        ingestor.flush_all()
        assert ingestor.list_projects() == ["p"]
        graph = ingestor.export_graph_to_dict()
        assert graph["metadata"]["total_nodes"] == 2
        assert graph["metadata"]["total_relationships"] == 1

    def test_generated_queries_run_but_cannot_write(self, ingestor) -> None:
        ingestor.ensure_node_batch("Project", {"name": "p"})
        ingestor.flush_all()
        assert ingestor.fetch_read_only("MATCH (p:Project) RETURN p.name AS name") == [
            {"name": "p"}
        ]
        with pytest.raises(ReadOnlyQueryError):
            ingestor.fetch_read_only("MATCH (p:Project) SET p.name = 'x' RETURN p")

    def test_the_embedded_engine_flushes_on_one_connection(self, ingestor) -> None:
        # One SQLite file takes one writer at a time; a pool of flush
        # workers would only queue on its lock.
        assert ingestor._executor is None

    def test_the_graph_lives_under_cgr_home_by_default(self, tmp_path, monkeypatch):
        from codebase_rag.config import settings

        monkeypatch.setattr(settings, "CGR_HOME", tmp_path)
        monkeypatch.setattr(settings, "SQLITE_GRAPH_PATH", None)
        assert settings.graph_db_file == tmp_path / "graph.sqlite"
