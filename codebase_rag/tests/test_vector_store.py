from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.utils.dependencies import has_pgvector_client

if TYPE_CHECKING:
    from pgvector.psycopg import Connection


@pytest.fixture
def mock_pgvector_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    return mock_client


@pytest.fixture
def reset_global_client() -> Generator[None, None, None]:
    import codebase_rag.vector_store as vs

    if has_pgvector_client():
        import codebase_rag.vector_store as vs
        vs.close_pgvector_client()
    vs.close_pgvector_client()

@pytest.fixture
def temp_pgvector_path() -> Generator[Path, None, None]:
    temp_dir = tempfile.mkdtemp(prefix="pgvector_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def integration_client(
    temp_pgvector_path: Path, reset_global_client: None
    ) -> Generator[Connection, None, None]:
    if not has_pgvector_client():
        pytest.skip("pgvector-client not installed")

    import codebase_rag.vector_store as vs
    yield vs.get_pgvector_client()


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_store_embedding_calls_upsert(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import store_embedding

    node_id = 123
    embedding = [0.1] * 768
    qualified_name = "myproject.module.function"

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        store_embedding(node_id, embedding, qualified_name)

    mock_pgvector_client.upsert.assert_called_once()
    call_kwargs = mock_pgvector_client.upsert.call_args[1]
    assert call_kwargs["collection_name"] == "code_embeddings"
    points = call_kwargs["points"]
    assert len(points) == 1
    assert points[0].id == node_id
    assert points[0].vector == embedding
    assert points[0].payload["node_id"] == node_id
    assert points[0].payload["qualified_name"] == qualified_name

@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_store_embedding_handles_exception(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import store_embedding

    mock_pgvector_client.upsert.side_effect = Exception("Connection failed")

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        store_embedding(123, [0.1] * 768, "test.func")


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_search_embeddings_calls_query_points(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_point1 = MagicMock()
    mock_point1.payload = {"node_id": 1}
    mock_point1.score = 0.95

    mock_point2 = MagicMock()
    mock_point2.payload = {"node_id": 2}
    mock_point2.score = 0.85

    mock_result = MagicMock()
    mock_result.points = [mock_point1, mock_point2]
    mock_pgvector_client.query_points.return_value = mock_result

    query_embedding = [0.2] * 768

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        results = search_embeddings(query_embedding, top_k=5)

    mock_pgvector_client.query_points.assert_called_once_with(
        collection_name="code_embeddings", query=query_embedding, limit=5
    )
    assert results == [(1, 0.95), (2, 0.85)]


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_search_embeddings_filters_null_payloads(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_point1 = MagicMock()
    mock_point1.payload = {"node_id": 1}
    mock_point1.score = 0.95

    mock_point2 = MagicMock()
    mock_point2.payload = None
    mock_point2.score = 0.85

    mock_result = MagicMock()
    mock_result.points = [mock_point1, mock_point2]
    mock_pgvector_client.query_points.return_value = mock_result

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        results = search_embeddings([0.2] * 768)

    assert results == [(1, 0.95)]


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_search_embeddings_handles_exception(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_pgvector_client.query_points.side_effect = Exception("Connection failed")

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        results = search_embeddings([0.2] * 768)

    assert results == []


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_search_embeddings_default_top_k(
    mock_pgvector_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_result = MagicMock()
    mock_result.points = []
    mock_pgvector_client.query_points.return_value = mock_result

    with patch(
        "codebase_rag.vector_store.get_pgvector_client",
        return_value=mock_pgvector_client,
    ):
        search_embeddings([0.2] * 768)

    mock_pgvector_client.query_points.assert_called_once_with(
        collection_name="code_embeddings", query=[0.2] * 768, limit=5
    )


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_store_and_search_roundtrip(integration_client: Connection) -> None:
    from codebase_rag.vector_store import search_embeddings, store_embedding

    embedding1 = [1.0] + [0.0] * 767
    embedding2 = [0.0, 1.0] + [0.0] * 766
    embedding3 = [0.9, 0.1] + [0.0] * 766

    store_embedding(1, embedding1, "project.module1.func1")
    store_embedding(2, embedding2, "project.module2.func2")
    store_embedding(3, embedding3, "project.module3.func3")

    query = [0.95, 0.05] + [0.0] * 766
    results = search_embeddings(query, top_k=3)

    assert len(results) == 3
    node_ids = [r[0] for r in results]
    assert node_ids[0] in [1, 3]
    assert node_ids[1] in [1, 3]


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_upsert_updates_existing(integration_client: Connection) -> None:
    from codebase_rag.vector_store import search_embeddings, store_embedding

    embedding_v1 = [1.0] + [0.0] * 767
    embedding_v2 = [0.0, 1.0] + [0.0] * 766

    store_embedding(1, embedding_v1, "project.func")
    store_embedding(1, embedding_v2, "project.func_updated")

    query = [0.0, 1.0] + [0.0] * 766
    results = search_embeddings(query, top_k=1)

    assert len(results) == 1
    assert results[0][0] == 1
    assert results[0][1] > 0.99


@pytest.mark.skipif(not has_pgvector_client(), reason="pgvector-client not installed")
def test_empty_search_returns_empty_list(integration_client: Connection) -> None:
    from codebase_rag.vector_store import search_embeddings

    results = search_embeddings([0.5] * 768, top_k=5)
    assert results == []
