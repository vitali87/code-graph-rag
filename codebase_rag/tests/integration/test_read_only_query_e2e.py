"""`fetch_read_only` against real engines: a generated query cannot write.

Both engines plan the query with EXPLAIN first, so the plan guard refuses
writes however the query text disguises them. Neo4j's READ access-mode
session is kept, but it is routing, not access control, so the tests expect
the plan guard's refusal on Neo4j too. The Neo4j tests skip without Docker or the
optional `neo4j` package; `neo4j_container` handles both.
"""

from __future__ import annotations

import pytest

from codebase_rag import exceptions as ex
from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = pytest.mark.integration

BACKENDS = ("memgraph_ingestor", "neo4j_ingestor")


def _node_count(ingestor: MemgraphIngestor) -> int:
    count = ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")[0]["c"]
    assert isinstance(count, int)
    return count


@pytest.fixture(params=BACKENDS)
def ingestor(request: pytest.FixtureRequest) -> MemgraphIngestor:
    ingestor: MemgraphIngestor = request.getfixturevalue(request.param)
    ingestor.execute_write("CREATE (:Function {name: 'delete'})")
    return ingestor


def test_read_query_returns_rows(ingestor: MemgraphIngestor) -> None:
    rows = ingestor.fetch_read_only(
        "MATCH (f:Function) WHERE f.name = 'delete' RETURN f.name AS name"
    )
    assert rows == [{"name": "delete"}]


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) DETACH DELETE n",
        "MATCH (n) SET n.pwned = true",
        "MERGE (:Pwned)",
        "MATCH (n) CALL { WITH n CREATE (x:Pwned) RETURN x } RETURN x",
        "MATCH (n) FOREACH (i IN [1] | CREATE (:Pwned))",
    ],
)
def test_write_query_is_refused_and_changes_nothing(
    ingestor: MemgraphIngestor, query: str
) -> None:
    before = _node_count(ingestor)
    # The plan check refuses it on both engines, before anything runs; on
    # Neo4j it must not depend on the READ session, which is only routing.
    with pytest.raises(ex.ReadOnlyQueryError):
        ingestor.fetch_read_only(query)
    assert _node_count(ingestor) == before
    assert ingestor.fetch_all("MATCH (n) WHERE n.pwned RETURN n") == []


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (n) CALL `mg.create_module_file`('pwned.py', 'x') "
        "YIELD path RETURN path",
        "MATCH (n) CALL /*c*/ mg.create_module_file('pwned.py', 'x') "
        "YIELD path RETURN path",
    ],
)
def test_memgraph_refuses_a_disguised_procedure_before_running_it(
    memgraph_ingestor: MemgraphIngestor, query: str
) -> None:
    with pytest.raises(ex.ReadOnlyQueryError, match="mg.create_module_file"):
        memgraph_ingestor.fetch_read_only(query)
    modules = memgraph_ingestor.fetch_all(
        "CALL mg.get_module_files() YIELD path RETURN path"
    )
    assert not any("pwned" in str(row["path"]) for row in modules)


# The plan guard is an allowlist of read operators, so an ordinary read that
# plans an operator it has never seen would be refused. These are the query
# shapes the Cypher prompt asks for; each must still run on both engines.
_ORDINARY_READS = [
    "MATCH (c:Class) RETURN count(c) AS total",
    "MATCH (f:Function) WHERE f.name = 'delete' RETURN f.qualified_name AS qn",
    "MATCH (f:Function) WHERE f.qualified_name STARTS WITH 'p.' "
    "RETURN f.name AS n LIMIT 5",
    "MATCH (n:Function|Method) RETURN n.name AS n",
    "MATCH (caller)-[r:CALLS]->(callee:Function) WHERE callee.name = 'delete' "
    "RETURN caller.name AS c, type(r) AS relationship",
    "MATCH (c:Function)-[r:CALLS]->(f:Function) RETURN f.name AS name, "
    "count(r) AS callers ORDER BY callers DESC LIMIT 10",
    "MATCH p = (a:Function)-[:CALLS*1..6]->(b:Function) RETURN length(p) AS l LIMIT 5",
    "MATCH p = (a:Function)-[:CALLS*1..6]->(a) RETURN p LIMIT 5",
    "MATCH (m:Module) OPTIONAL MATCH (m)-[:DEFINES]->(f:Function) "
    "RETURN m.name AS m, collect(f.name) AS fs",
    "UNWIND ['a', 'b'] AS x MATCH (f:Function {name: x}) RETURN DISTINCT f.name AS n",
    "MATCH (f:Function) RETURN f.name AS n UNION MATCH (m:Method) RETURN m.name AS n",
    "MATCH (f:Function) WITH f ORDER BY f.name LIMIT 3 RETURN collect(f.name) AS ns",
    "MATCH (f:Function) WHERE f.name IN ['a', 'delete'] RETURN f.name AS n SKIP 0",
    "MATCH (f:Function) CALL { WITH f MATCH (f)-[:CALLS]->(g) RETURN count(g) AS c } "
    "RETURN f.name AS n, c",
    "MATCH ()-[r:CALLS]->() RETURN count(r) AS c",
    "MATCH (a:Function)-[r:CALLS]-(b) RETURN a.name AS a, b.name AS b LIMIT 1",
]


@pytest.mark.parametrize("query", _ORDINARY_READS)
def test_ordinary_reads_pass_the_plan_guard(
    ingestor: MemgraphIngestor, query: str
) -> None:
    ingestor.fetch_read_only(query)


def test_breadth_first_expansion_passes_on_memgraph(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    memgraph_ingestor.fetch_read_only(
        "MATCH (a:Function)-[:CALLS *BFS ..5]->(b) RETURN b.name AS n LIMIT 1"
    )
