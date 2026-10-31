"""On Memgraph, a row without a site leaves the located edges alone (issue #2879).

There a MERGE on the endpoints alone matched every parallel CALLS edge of the
pair, so a site-less row set its properties on each site: a guessed call read
as `exact`.
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import PropertyDict

pytestmark = pytest.mark.integration

_FUNCTION = cs.NodeLabel.FUNCTION.value
_CALLS = cs.RelationshipType.CALLS.value
_EXACT: PropertyDict = {cs.KEY_RESOLUTION: cs.EdgeResolution.EXACT.value}


def _fn(qn: str) -> tuple[str, str, str]:
    return (_FUNCTION, cs.KEY_QUALIFIED_NAME, qn)


def _call(ingestor: MemgraphIngestor, props: PropertyDict) -> None:
    ingestor.ensure_relationship_batch(_fn("m.a"), _CALLS, _fn("m.b"), dict(props))


@pytest.fixture
def ingestor(memgraph_ingestor: MemgraphIngestor) -> MemgraphIngestor:
    memgraph_ingestor._execute_query(
        "CREATE (:Function {qualified_name: 'm.a'}), "
        "(:Function {qualified_name: 'm.b'})"
    )
    return memgraph_ingestor


def _edges(ingestor: MemgraphIngestor) -> list[tuple[object, object, object]]:
    rows = ingestor.fetch_all(
        "MATCH (:Function)-[r:CALLS]->(:Function) "
        "RETURN r.line AS line, r.resolution AS resolution, "
        "r.dynamic_call_count AS count"
    )
    return sorted(
        ((r["line"], r["resolution"], r["count"]) for r in rows),
        key=lambda e: str(e[0]),
    )


def test_a_row_without_a_site_leaves_the_located_edges_alone(
    ingestor: MemgraphIngestor,
) -> None:
    for line in (4, 9):
        _call(
            ingestor,
            {
                cs.KEY_LINE: line,
                cs.KEY_COL: 4,
                cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC.value,
            },
        )
    ingestor.flush_relationships()
    _call(ingestor, _EXACT)
    _call(ingestor, {**_EXACT, "again": True})
    ingestor.flush_relationships()

    assert _edges(ingestor) == [
        (4, "heuristic", None),
        (9, "heuristic", None),
        (None, "exact", None),
    ]
