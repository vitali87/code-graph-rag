"""Project ownership queries on the embedded engine, the default backend.

`delete-project` and `stats` walk what a project owns (issue #2665). Written
with a path variable, that walk compiled on Memgraph only, so on the default
backend both commands failed with "unsupported in the embedded graph engine:
path variables". The graph is one checkout indexed under two names: `shop`
and `old` share the `src` Folder and the File under it, and each has its own
Package and Module.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from codebase_rag import cypher_queries as cq
from codebase_rag.embedded_graph.connection import EmbeddedConnection

_GRAPH = """
CREATE (shop:Project {name: 'shop', root_path: '/w'}),
       (old:Project {name: 'old', root_path: '/w'}),
       (src:Folder {absolute_path: '/w/src'}),
       (cart:File {absolute_path: '/w/src/cart.py'}),
       (sp:Package {qualified_name: 'shop.src'}),
       (op:Package {qualified_name: 'old.src'}),
       (sm:Module {qualified_name: 'shop.src.cart'}),
       (om:Module {qualified_name: 'old.src.cart'}),
       (price:Function {qualified_name: 'shop.src.cart.price'}),
       (shop)-[:CONTAINS_FOLDER]->(src), (old)-[:CONTAINS_FOLDER]->(src),
       (src)-[:CONTAINS_PACKAGE]->(sp), (src)-[:CONTAINS_PACKAGE]->(op),
       (sp)-[:CONTAINS_FILE]->(cart), (op)-[:CONTAINS_FILE]->(cart),
       (sp)-[:CONTAINS_MODULE]->(sm), (op)-[:CONTAINS_MODULE]->(om),
       (sm)-[:DEFINES]->(price)
"""
_KEYS = (
    "MATCH (n) RETURN coalesce(n.qualified_name, n.absolute_path, n.name) AS k "
    "ORDER BY k"
)
_SHOP = [
    "/w/src",
    "/w/src/cart.py",
    "shop",
    "shop.src",
    "shop.src.cart",
    "shop.src.cart.price",
]


@pytest.fixture
def conn() -> Iterator[EmbeddedConnection]:
    with EmbeddedConnection(":memory:") as c:
        c.run(_GRAPH)
        yield c


def test_the_per_project_breakdown_counts_each_name_once(
    conn: EmbeddedConnection,
) -> None:
    rows = conn.run(cq.CYPHER_STATS_PER_PROJECT).rows
    # Each name counts its own Project, Package and Module plus the shared
    # Folder and File, never the other name's Package or Module.
    assert [(project, nodes) for project, nodes, _ in rows] == [("old", 5), ("shop", 6)]


def test_scoped_counts_leave_out_the_other_name(conn: EmbeddedConnection) -> None:
    params = {"project_names": ["shop"]}
    nodes = conn.run(cq.CYPHER_STATS_PROJECT_NODE_COUNTS, params).rows
    rels = conn.run(cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS, params).rows
    assert sorted(str(labels) for labels, _ in nodes) == [
        "['File']",
        "['Folder']",
        "['Function']",
        "['Module']",
        "['Package']",
        "['Project']",
    ]
    assert [count for _, count in nodes] == [1] * len(_SHOP)
    assert {str(kind): count for kind, count in rels} == {
        "CONTAINS_FOLDER": 1,
        "CONTAINS_PACKAGE": 1,
        "CONTAINS_FILE": 1,
        "CONTAINS_MODULE": 1,
        "DEFINES": 1,
    }


def test_deleting_one_name_keeps_what_the_other_shares(
    conn: EmbeddedConnection,
) -> None:
    conn.run(cq.CYPHER_DELETE_PROJECT, {"project_name": "old"})
    conn.run(
        cq.CYPHER_DELETE_PROJECT_CUT_OFF_UNITS,
        {"project_name": "old", "project_prefix": "old."},
    )
    assert [row[0] for row in conn.run(_KEYS).rows] == _SHOP


def test_deleting_the_last_name_takes_the_shared_nodes(
    conn: EmbeddedConnection,
) -> None:
    # Negative: with no other name left to claim them, the Folder and File go.
    for name in ("old", "shop"):
        conn.run(cq.CYPHER_DELETE_PROJECT, {"project_name": name})
    assert conn.run(_KEYS).rows == []
