"""A relationship flush counts the rows it wrote, not the edges it matched.

A row without the per-call-site keys MERGEs on its endpoints alone, which
matches every parallel CALLS edge between the pair. `count(r)` then counted
each matched edge, so a 12-row batch reported 14 written and the summary
logged "702 relationships (704 successful, -2 failed)" (issue #2879).
"""

from __future__ import annotations

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyValue

pytestmark = pytest.mark.integration

_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value


def _fn(qn: str) -> tuple[str, str, str]:
    return (_FUNCTION, cs.KEY_QUALIFIED_NAME, qn)


def _flush_summary(ingestor: MemgraphIngestor) -> str:
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(str(m)), level="INFO")
    try:
        ingestor.flush_relationships()
    finally:
        logger.remove(handler)
    summaries = [line for line in lines if "relationships (" in line]
    assert len(summaries) == 1, lines
    return summaries[0]


def test_a_row_matching_parallel_edges_counts_once(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    memgraph_ingestor._execute_query(
        "CREATE (:Function {qualified_name: 'm.caller'}), "
        "(:Function {qualified_name: 'm.callee'}), "
        "(:Function {qualified_name: 'm.other'})"
    )
    # Three call sites of one pair, one edge each.
    for line in (34, 189, 195):
        memgraph_ingestor.ensure_relationship_batch(
            _fn("m.caller"),
            _CALLS,
            _fn("m.callee"),
            {cs.KEY_LINE: line, cs.KEY_COL: 4, cs.KEY_RESOLUTION: "exact"},
        )
    memgraph_ingestor.flush_relationships()

    # The issue's batch: a site-less row for that pair, beside a row whose
    # callee does not exist and the same new edge written twice.
    exact: dict[str, PropertyValue] = {cs.KEY_RESOLUTION: "exact"}
    for to_qn in ("m.callee", "m.missing", "m.other", "m.other"):
        memgraph_ingestor.ensure_relationship_batch(
            _fn("m.caller"), _CALLS, _fn(to_qn), dict(exact)
        )

    summary = _flush_summary(memgraph_ingestor)
    assert "Flushed 4 relationships (3 successful, 1 failed)." in summary, summary


def test_a_batch_of_single_edges_still_counts_every_row(
    memgraph_ingestor: MemgraphIngestor,
) -> None:
    # Negative: the common batch, one new edge per row, is unchanged.
    memgraph_ingestor._execute_query(
        "UNWIND range(1, 5) AS i CREATE (:Function {qualified_name: 'f' + toString(i)})"
    )
    for i in range(1, 5):
        memgraph_ingestor.ensure_relationship_batch(
            _fn(f"f{i}"), _CALLS, _fn(f"f{i + 1}"), {cs.KEY_LINE: i, cs.KEY_COL: 0}
        )
    summary = _flush_summary(memgraph_ingestor)
    assert "Flushed 4 relationships (4 successful, 0 failed)." in summary, summary
