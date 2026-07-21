"""Semantic search must support restricting results to one project (issue #425).

With several repositories indexed into one graph, an unfiltered vector search
mixes hits from every project. Passing a project name must confine results to
qualified names under that project's prefix, on both vector-store backends.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import VectorStoreBackend
from codebase_rag.utils.dependencies import (
    has_pymilvus,
    has_qdrant_client,
    has_semantic_dependencies,
)

_EMBEDDING = [0.2] * 768


def _qdrant_point(node_id: int, qualified_name: str, score: float) -> MagicMock:
    point = MagicMock()
    point.payload = {"node_id": node_id, "qualified_name": qualified_name}
    point.score = score
    return point


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_qdrant_search_filters_by_project_prefix() -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.points = [
        _qdrant_point(1, "user-service__aaaa1111.src.handlers.get_user", 0.95),
        _qdrant_point(2, "order-service__bbbb2222.src.orders.create", 0.90),
        _qdrant_point(3, "user-service__aaaa1111.src.models.User", 0.85),
    ]
    mock_client.query_points.return_value = mock_result

    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT),
        patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client),
    ):
        vs.close_vector_store_client()
        results = vs.search_embeddings(
            _EMBEDDING, top_k=2, project="user-service__aaaa1111"
        )
        vs.close_vector_store_client()

    assert results == [(1, 0.95), (3, 0.85)]


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_qdrant_search_overfetches_when_project_given() -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.points = []
    mock_client.query_points.return_value = mock_result

    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT),
        patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client),
    ):
        vs.close_vector_store_client()
        vs.search_embeddings(_EMBEDDING, top_k=5, project="proj__12345678")
        vs.close_vector_store_client()

    limit = mock_client.query_points.call_args.kwargs["limit"]
    assert limit > 5, "project filter must over-fetch to compensate for filtering"


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_qdrant_search_without_project_keeps_plain_limit() -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.points = []
    mock_client.query_points.return_value = mock_result

    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT),
        patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client),
    ):
        vs.close_vector_store_client()
        vs.search_embeddings(_EMBEDDING, top_k=5)
        vs.close_vector_store_client()

    assert mock_client.query_points.call_args.kwargs["limit"] == 5


def _milvus_hit(node_id: int, qualified_name: str, distance: float) -> dict:
    return {
        "id": node_id,
        "distance": distance,
        "entity": {"node_id": node_id, "qualified_name": qualified_name},
    }


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
def test_milvus_search_filters_by_project_prefix() -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_client.search.return_value = [
        [
            _milvus_hit(1, "user-service__aaaa1111.src.handlers.get_user", 0.1),
            _milvus_hit(2, "order-service__bbbb2222.src.orders.create", 0.2),
            _milvus_hit(3, "user-service__aaaa1111.src.models.User", 0.3),
        ]
    ]

    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.MILVUS),
        patch("codebase_rag.vector_store.get_milvus_client", return_value=mock_client),
    ):
        vs.close_vector_store_client()
        results = vs.search_embeddings(
            _EMBEDDING, top_k=2, project="user-service__aaaa1111"
        )
        vs.close_vector_store_client()

    assert [node_id for node_id, _ in results] == [1, 3]
    output_fields = mock_client.search.call_args.kwargs["output_fields"]
    assert "qualified_name" in output_fields


@pytest.mark.skipif(not has_semantic_dependencies(), reason="semantic deps missing")
def test_semantic_code_search_passes_project_to_vector_search() -> None:
    from codebase_rag.tools.semantic_search import semantic_code_search

    mock_search = MagicMock(return_value=[])
    with (
        patch("codebase_rag.embedder.embed_code", return_value=_EMBEDDING),
        patch("codebase_rag.vector_store.search_embeddings", mock_search),
    ):
        semantic_code_search(
            MagicMock(), "find user handler", top_k=7, project="proj__12345678"
        )

    assert mock_search.call_args.kwargs["project"] == "proj__12345678"
    assert mock_search.call_args.kwargs["top_k"] == 7


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_qdrant_widens_window_until_project_matches_found() -> None:
    # The first over-fetch window may hold only other projects' neighbors;
    # the search must widen the window instead of returning nothing.
    import codebase_rag.vector_store as vs

    other = [
        _qdrant_point(i, f"noise__00000000.mod.f{i}", 0.9 - i * 0.001)
        for i in range(100)
    ]
    wanted = [
        _qdrant_point(1000, "user-service__aaaa1111.src.handlers.get_user", 0.5),
        _qdrant_point(1001, "user-service__aaaa1111.src.models.User", 0.4),
    ]

    def query_points(collection_name: str, query: list[float], limit: int):  # type: ignore[no-untyped-def]
        result = MagicMock()
        result.points = (other + wanted)[:limit]
        return result

    mock_client = MagicMock()
    mock_client.query_points.side_effect = query_points

    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT),
        patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client),
    ):
        vs.close_vector_store_client()
        results = vs.search_embeddings(
            _EMBEDDING, top_k=2, project="user-service__aaaa1111"
        )
        vs.close_vector_store_client()

    assert [node_id for node_id, _ in results] == [1000, 1001]
