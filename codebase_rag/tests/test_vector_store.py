from __future__ import annotations

import shutil
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import VectorStoreBackend
from codebase_rag.utils.dependencies import has_pymilvus, has_qdrant_client

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


@pytest.fixture(autouse=True)
def use_qdrant_backend() -> Generator[None, None, None]:
    import codebase_rag.vector_store as vs

    with patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT):
        yield


@pytest.fixture
def mock_qdrant_client() -> MagicMock:
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = True
    return mock_client


@pytest.fixture
def reset_global_client() -> Generator[None, None, None]:
    import codebase_rag.vector_store as vs

    vs.close_vector_store_client()

    yield

    vs.close_vector_store_client()


@pytest.fixture
def temp_qdrant_path() -> Generator[Path, None, None]:
    temp_dir = tempfile.mkdtemp(prefix="qdrant_test_")
    yield Path(temp_dir)
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def integration_client(
    temp_qdrant_path: Path, reset_global_client: None
) -> Generator[QdrantClient, None, None]:
    if not has_qdrant_client():
        pytest.skip("qdrant-client not installed")

    from qdrant_client import QdrantClient as QC
    from qdrant_client.models import Distance, VectorParams

    import codebase_rag.vector_store as vs

    client = QC(path=str(temp_qdrant_path))
    client.create_collection(
        collection_name="code_embeddings",
        vectors_config=VectorParams(size=768, distance=Distance.COSINE),
    )
    vs._CLIENT = client
    vs._CLIENT_BACKEND = VectorStoreBackend.QDRANT

    yield client

    vs.close_vector_store_client()


def test_get_qdrant_client_uses_url_when_set(reset_global_client: None) -> None:
    import codebase_rag.vector_store as vs

    with patch.object(vs.settings, "QDRANT_URL", "http://localhost:6333"):
        with patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls:
            instance = MagicMock()
            instance.collection_exists.return_value = True
            mock_client_cls.return_value = instance
            vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(url="http://localhost:6333")


def test_get_qdrant_client_uses_path_when_url_unset(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with patch.object(vs.settings, "QDRANT_URL", None):
        with patch.object(vs.settings, "QDRANT_DB_PATH", "/tmp/qd"):
            with patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls:
                instance = MagicMock()
                instance.collection_exists.return_value = True
                mock_client_cls.return_value = instance
                vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(path="/tmp/qd")


def test_get_qdrant_client_logs_and_reraises_on_lock_error(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with patch.object(vs.settings, "QDRANT_URL", None):
        with patch.object(vs.settings, "QDRANT_DB_PATH", "/tmp/qd_locked"):
            with patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls:
                mock_client_cls.side_effect = RuntimeError(
                    "Storage folder is already accessed by another instance"
                )
                with patch("codebase_rag.vector_store.logger") as mock_logger:
                    with pytest.raises(RuntimeError):
                        vs.get_qdrant_client()

    mock_logger.error.assert_called_once()


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_store_embedding_calls_upsert(
    mock_qdrant_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import store_embedding

    node_id = 123
    embedding = [0.1] * 768
    qualified_name = "myproject.module.function"

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        store_embedding(node_id, embedding, qualified_name)

    mock_qdrant_client.upsert.assert_called_once()
    call_kwargs = mock_qdrant_client.upsert.call_args[1]
    assert call_kwargs["collection_name"] == "code_embeddings"
    points = call_kwargs["points"]
    assert len(points) == 1
    assert points[0].id == node_id
    assert points[0].vector == embedding
    assert points[0].payload["node_id"] == node_id
    assert points[0].payload["qualified_name"] == qualified_name


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_store_embedding_handles_exception(
    mock_qdrant_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import store_embedding

    mock_qdrant_client.upsert.side_effect = Exception("Connection failed")

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        store_embedding(123, [0.1] * 768, "test.func")


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_search_embeddings_calls_query_points(
    mock_qdrant_client: MagicMock, reset_global_client: None
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
    mock_qdrant_client.query_points.return_value = mock_result

    query_embedding = [0.2] * 768

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        results = search_embeddings(query_embedding, top_k=5)

    mock_qdrant_client.query_points.assert_called_once_with(
        collection_name="code_embeddings", query=query_embedding, limit=5
    )
    assert results == [(1, 0.95), (2, 0.85)]


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_search_embeddings_filters_null_payloads(
    mock_qdrant_client: MagicMock, reset_global_client: None
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
    mock_qdrant_client.query_points.return_value = mock_result

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        results = search_embeddings([0.2] * 768)

    assert results == [(1, 0.95)]


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_search_embeddings_handles_exception(
    mock_qdrant_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_qdrant_client.query_points.side_effect = Exception("Connection failed")

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        results = search_embeddings([0.2] * 768)

    assert results == []


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_search_embeddings_default_top_k(
    mock_qdrant_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import search_embeddings

    mock_result = MagicMock()
    mock_result.points = []
    mock_qdrant_client.query_points.return_value = mock_result

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        search_embeddings([0.2] * 768)

    mock_qdrant_client.query_points.assert_called_once_with(
        collection_name="code_embeddings", query=[0.2] * 768, limit=5
    )


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_store_and_search_roundtrip(integration_client: QdrantClient) -> None:
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


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_upsert_updates_existing(integration_client: QdrantClient) -> None:
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


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_empty_search_returns_empty_list(integration_client: QdrantClient) -> None:
    from codebase_rag.vector_store import search_embeddings

    results = search_embeddings([0.5] * 768, top_k=5)
    assert results == []


def test_get_milvus_client_uses_uri_token_and_db(reset_global_client: None) -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_client.has_collection.return_value = True
    mock_client.describe_collection.return_value = {
        "fields": [
            {"name": "node_id"},
            {"name": "qualified_name"},
            {"name": "embedding", "params": {"dim": 768}},
        ]
    }

    with (
        patch.object(vs.settings, "MILVUS_URI", "http://localhost:19530"),
        patch.object(vs.settings, "MILVUS_TOKEN", "root:Milvus"),
        patch.object(vs.settings, "MILVUS_DB_NAME", "default"),
        patch(
            "codebase_rag.vector_store.MilvusClient", return_value=mock_client
        ) as mock_client_cls,
    ):
        client = vs.get_milvus_client()

    assert client is mock_client
    mock_client_cls.assert_called_once_with(
        uri="http://localhost:19530",
        token="root:Milvus",
        db_name="default",
    )


def test_milvus_empty_search_response_returns_empty_list() -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_client.search.return_value = []

    with (
        patch("codebase_rag.vector_store.get_milvus_client", return_value=mock_client),
        patch("codebase_rag.vector_store.logger") as mock_logger,
    ):
        results = vs.MilvusVectorStore().search_embeddings([0.2] * 768, top_k=5)

    assert results == []
    mock_logger.warning.assert_not_called()


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
def test_milvus_store_search_verify_delete_roundtrip(
    tmp_path: Path, reset_global_client: None
) -> None:
    import codebase_rag.vector_store as vs

    collection_name = "code_embeddings_test"
    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.MILVUS),
        patch.object(vs.settings, "MILVUS_URI", str(tmp_path / "milvus.db")),
        patch.object(vs.settings, "MILVUS_COLLECTION_NAME", collection_name),
        patch.object(vs.settings, "MILVUS_VECTOR_DIM", 4),
    ):
        stored = vs.store_embedding_batch(
            [
                (101, [1.0, 0.0, 0.0, 0.0], "pkg.auth.login"),
                (102, [0.0, 1.0, 0.0, 0.0], "pkg.billing.charge"),
                (103, [0.9, 0.1, 0.0, 0.0], "pkg.auth.refresh"),
            ]
        )
        vs.close_vector_store_client()
        results = vs.search_embeddings([0.95, 0.05, 0.0, 0.0], top_k=2)
        found_ids = vs.verify_stored_ids({101, 102, 999})
        vs.delete_project_embeddings("pkg", [101, 102])
        remaining_ids = vs.verify_stored_ids({101, 102, 103})

    assert stored == 3
    assert [node_id for node_id, _score in results] == [101, 103]
    assert found_ids == {101, 102}
    assert remaining_ids == {103}
