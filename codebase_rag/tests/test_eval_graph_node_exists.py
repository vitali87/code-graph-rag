# The in-memory store answers CYPHER_GRAPH_NODE_EXISTS for the unknown-target
# refusals in `cgr graph` and the MCP tools: a name counts as known when any
# node in any project carries it, and the answer is at most one row (#3436).
from __future__ import annotations

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.graph_query import node_exists
from evals.cgr_graph import _StatefulIngestor

QN = "other_project.pkg.mod.run"


def _store() -> _StatefulIngestor:
    store = _StatefulIngestor()
    # The same name under two labels: two nodes, so two candidate rows.
    for label in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
        store.ensure_node_batch(label.value, {cs.KEY_QUALIFIED_NAME: QN})
    return store


def test_a_name_any_node_carries_is_found_as_one_row() -> None:
    store = _store()
    rows = store.fetch_all(cq.CYPHER_GRAPH_NODE_EXISTS, {cs.KEY_QN: QN})
    assert rows == [{cs.KEY_QUALIFIED_NAME: QN}]
    assert node_exists(store.fetch_all, QN)


def test_an_unknown_name_is_not_found() -> None:
    store = _store()
    missing = "other_project.pkg.mod.walk"
    assert store.fetch_all(cq.CYPHER_GRAPH_NODE_EXISTS, {cs.KEY_QN: missing}) == []
    assert not node_exists(store.fetch_all, missing)
