from __future__ import annotations

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph import GraphIngestor

pytestmark = [pytest.mark.integration]


def test_graph_ingestor_fixture_satisfies_protocol(
    graph_ingestor: GraphIngestor,
) -> None:
    assert isinstance(graph_ingestor, GraphIngestor)


def test_graph_container_reports_its_backend(
    graph_container: dict[str, object],
) -> None:
    assert graph_container["backend"] in set(GraphBackend)


def test_fixture_starts_empty(graph_ingestor: GraphIngestor) -> None:
    rows = graph_ingestor.fetch_all("MATCH (n) RETURN count(n) AS c")
    assert rows[0]["c"] == 0
