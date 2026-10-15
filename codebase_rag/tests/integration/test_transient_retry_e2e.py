"""Issue #2441 against a real Memgraph: a write another open transaction
conflicts with goes through once that transaction commits.

Memgraph rejects the second writer at once with "Cannot resolve conflicting
transactions. Retry this transaction when the conflicting transaction is
finished"; the ingestor used to end the run there with a traceback.
"""

from __future__ import annotations

import threading

import mgclient
import pytest

from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]


def test_a_conflicting_write_goes_through_after_the_other_commits(
    memgraph_ingestor: MemgraphIngestor,
    memgraph_container: dict[str, str | int],
) -> None:
    memgraph_ingestor.execute_write("CREATE (:Probe {id: 1, v: 0})")
    other = mgclient.connect(
        host=str(memgraph_container["host"]), port=int(memgraph_container["port"])
    )
    try:
        # Not autocommit: this write stays open, holding the node.
        other.cursor().execute("MATCH (n:Probe {id: 1}) SET n.v = 1")
        commit = threading.Timer(0.5, other.commit)
        commit.start()

        memgraph_ingestor.execute_write("MATCH (n:Probe {id: 1}) SET n.v = 2")
        commit.join()
    finally:
        other.close()

    rows = memgraph_ingestor.fetch_all("MATCH (n:Probe {id: 1}) RETURN n.v AS v")
    assert rows == [{"v": 2}]


def test_a_query_error_is_still_raised_at_once(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # Negative.
    with pytest.raises(mgclient.DatabaseError):
        memgraph_ingestor.execute_write("MATC (n) RETURN n")
