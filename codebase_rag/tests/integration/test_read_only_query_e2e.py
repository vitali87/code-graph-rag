"""`fetch_read_only` against real engines: a generated query cannot write.

Memgraph has no read-only session, so this checks the EXPLAIN plan guard
refuses writes however the query text disguises them. Neo4j enforces a READ
access-mode session server-side. The Neo4j tests skip without Docker or the
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
    # ReadOnlyQueryError on Memgraph, the driver's ClientError on Neo4j.
    with pytest.raises(Exception):
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
