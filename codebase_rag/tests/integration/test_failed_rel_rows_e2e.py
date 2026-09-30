# Issue #2438, against Memgraph: the endpoint lookup a lossy relationship flush
# runs is valid on the shipped engine, and it names exactly the rows the write
# lost, not the head of the batch.
from __future__ import annotations

from collections.abc import Generator

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

CALLER = "proj.app.caller"
PHANTOM = "builtin.Error.prototype.customDelay"
CALLS_FROM_CALLER = (
    "MATCH (:Function {qualified_name: $qn})-[:CALLS]->(t) "
    "RETURN t.qualified_name AS qn ORDER BY qn"
)


@pytest.fixture
def warnings() -> Generator[list[str], None, None]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    yield messages
    logger.remove(sink)


def _flush_calls(ingestor: MemgraphIngestor, targets: list[str]) -> None:
    for qn in (CALLER, "proj.app.ok_1", "proj.app.ok_2"):
        ingestor.ensure_node_batch(
            cs.NodeLabel.FUNCTION,
            {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_NAME: qn.rsplit(".", 1)[-1]},
        )
    ingestor.flush_nodes()
    for target in targets:
        ingestor.ensure_relationship_batch(
            (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, CALLER),
            cs.RelationshipType.CALLS,
            (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target),
        )
    ingestor.flush_relationships()


def _called(ingestor: MemgraphIngestor) -> list[str]:
    return [str(r["qn"]) for r in ingestor.fetch_all(CALLS_FROM_CALLER, {"qn": CALLER})]


def test_a_lossy_flush_names_only_the_lost_row(
    memgraph_ingestor: MemgraphIngestor, warnings: list[str]
) -> None:
    _flush_calls(memgraph_ingestor, ["proj.app.ok_1", PHANTOM, "proj.app.ok_2"])

    rows = [w for w in warnings if w.startswith("  Failed ")]
    assert len(rows) == 1, warnings
    assert PHANTOM in rows[0], rows
    assert "missing target" in rows[0], rows
    assert any("Failed to create 1 of 3" in w for w in warnings), warnings
    assert _called(memgraph_ingestor) == ["proj.app.ok_1", "proj.app.ok_2"]


def test_a_complete_flush_warns_about_nothing(
    memgraph_ingestor: MemgraphIngestor, warnings: list[str]
) -> None:
    # Negative.
    _flush_calls(memgraph_ingestor, ["proj.app.ok_1", "proj.app.ok_2"])

    assert warnings == []
    assert _called(memgraph_ingestor) == ["proj.app.ok_1", "proj.app.ok_2"]
