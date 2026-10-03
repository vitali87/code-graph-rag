from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.types_defs import EmbeddingSymbol
from codebase_rag.utils.dependencies import has_pymilvus, has_qdrant_client

if TYPE_CHECKING:
    from qdrant_client import QdrantClient


def _fn(qualified_name: str) -> EmbeddingSymbol:
    return EmbeddingSymbol(cs.NodeLabel.FUNCTION, qualified_name)


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

    mock_client_cls.assert_called_once_with(url="http://localhost:6333", api_key=None)


def test_get_qdrant_client_passes_api_key_to_server(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "QDRANT_URL", "https://qdrant.example:6333"),
        patch.object(vs.settings, "QDRANT_API_KEY", "secret-key"),
        patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.collection_exists.return_value = True
        vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(
        url="https://qdrant.example:6333", api_key="secret-key"
    )


def test_get_qdrant_client_refuses_api_key_over_http(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "QDRANT_URL", "http://qdrant.example:6333"),
        patch.object(vs.settings, "QDRANT_API_KEY", "secret-key"),
        patch.object(vs.settings, "QDRANT_ALLOW_INSECURE_API_KEY", False),
        patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls,
    ):
        with pytest.raises(ValueError, match="QDRANT_ALLOW_INSECURE_API_KEY"):
            vs.get_qdrant_client()

    mock_client_cls.assert_not_called()


def test_get_qdrant_client_sends_api_key_over_http_when_allowed(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "QDRANT_URL", "http://localhost:6333"),
        patch.object(vs.settings, "QDRANT_API_KEY", "secret-key"),
        patch.object(vs.settings, "QDRANT_ALLOW_INSECURE_API_KEY", True),
        patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.collection_exists.return_value = True
        vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(
        url="http://localhost:6333", api_key="secret-key"
    )


def test_get_qdrant_client_treats_blank_api_key_as_unset(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "QDRANT_URL", "http://localhost:6333"),
        patch.object(vs.settings, "QDRANT_API_KEY", ""),
        patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.collection_exists.return_value = True
        vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(url="http://localhost:6333", api_key=None)


def test_get_qdrant_client_ignores_api_key_in_local_mode(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "QDRANT_URL", None),
        patch.object(vs.settings, "QDRANT_DB_PATH", "/tmp/qd"),
        patch.object(vs.settings, "QDRANT_API_KEY", "secret-key"),
        patch("codebase_rag.vector_store.QdrantClient") as mock_client_cls,
    ):
        mock_client_cls.return_value.collection_exists.return_value = True
        vs.get_qdrant_client()

    mock_client_cls.assert_called_once_with(path="/tmp/qd")


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


def _open_local_qdrant_with_existing_collection(path: Path, size: int) -> None:
    from qdrant_client import QdrantClient as QC
    from qdrant_client.models import Distance, VectorParams

    client = QC(path=str(path))
    client.create_collection(
        collection_name="code_embeddings",
        vectors_config=VectorParams(size=size, distance=Distance.COSINE),
    )
    # Embedded Qdrant allows one process-level handle per folder, so release
    # it before get_qdrant_client opens the same path.
    client.close()


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_get_qdrant_client_rejects_existing_collection_with_other_dim(
    temp_qdrant_path: Path, reset_global_client: None
) -> None:
    import codebase_rag.vector_store as vs

    _open_local_qdrant_with_existing_collection(temp_qdrant_path, size=1536)

    with patch.object(vs.settings, "QDRANT_URL", None):
        with patch.object(vs.settings, "QDRANT_DB_PATH", str(temp_qdrant_path)):
            with patch.object(vs.settings, "QDRANT_VECTOR_DIM", 768):
                with pytest.raises(ValueError, match="dimension 1536, expected 768"):
                    vs.get_qdrant_client()

    # A rejected collection must not leave a half-initialised cached client
    # behind for the next caller to reuse without validation.
    assert vs._CLIENT is None


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_get_qdrant_client_accepts_existing_collection_with_same_dim(
    temp_qdrant_path: Path, reset_global_client: None
) -> None:
    import codebase_rag.vector_store as vs

    _open_local_qdrant_with_existing_collection(temp_qdrant_path, size=1536)

    with patch.object(vs.settings, "QDRANT_URL", None):
        with patch.object(vs.settings, "QDRANT_DB_PATH", str(temp_qdrant_path)):
            with patch.object(vs.settings, "QDRANT_VECTOR_DIM", 1536):
                client = vs.get_qdrant_client()

    assert client.collection_exists("code_embeddings")


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_clear_all_embeddings_recreates_collection_with_other_dim(
    temp_qdrant_path: Path, reset_global_client: None
) -> None:
    import codebase_rag.vector_store as vs

    _open_local_qdrant_with_existing_collection(temp_qdrant_path, size=1536)

    with patch.object(vs.settings, "QDRANT_URL", None):
        with patch.object(vs.settings, "QDRANT_DB_PATH", str(temp_qdrant_path)):
            with patch.object(vs.settings, "QDRANT_VECTOR_DIM", 768):
                vs.QdrantVectorStore().clear_all_embeddings()
                client = vs.get_qdrant_client()
                info = client.get_collection(collection_name="code_embeddings")

    assert info.config.params.vectors.size == 768


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
@pytest.mark.parametrize("failing", ["delete_collection", "create_collection"])
def test_a_failed_clear_does_not_leave_its_unvalidated_client_cached(
    reset_global_client: None, failing: str
) -> None:
    import codebase_rag.vector_store as vs

    instance = MagicMock()
    instance.collection_exists.return_value = True
    getattr(instance, failing).side_effect = RuntimeError("store is down")
    store = vs.QdrantVectorStore()
    with patch.object(vs.settings, "QDRANT_URL", "http://localhost:6333"):
        with patch("codebase_rag.vector_store.QdrantClient", return_value=instance):
            with pytest.raises(RuntimeError):
                store.clear_all_embeddings()

    assert vs._CLIENT is None
    instance.close.assert_called_once()


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_store_embedding_calls_upsert(
    mock_qdrant_client: MagicMock, reset_global_client: None
) -> None:
    from codebase_rag.vector_store import embedding_point_id, store_embedding

    node_id = 123
    embedding = [0.1] * 768
    symbol = _fn("myproject.module.function")

    with patch(
        "codebase_rag.vector_store.get_qdrant_client",
        return_value=mock_qdrant_client,
    ):
        store_embedding("myproject", node_id, embedding, symbol)

    mock_qdrant_client.upsert.assert_called_once()
    call_kwargs = mock_qdrant_client.upsert.call_args[1]
    assert call_kwargs["collection_name"] == "code_embeddings"
    points = call_kwargs["points"]
    assert len(points) == 1
    assert points[0].id == embedding_point_id("myproject", symbol)
    assert points[0].vector == embedding
    assert points[0].payload["node_id"] == node_id
    assert points[0].payload["qualified_name"] == symbol.qualified_name
    assert points[0].payload["project"] == "myproject"


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
        store_embedding("test", 123, [0.1] * 768, _fn("test.func"))


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

    store_embedding("project", 1, embedding1, _fn("project.module1.func1"))
    store_embedding("project", 2, embedding2, _fn("project.module2.func2"))
    store_embedding("project", 3, embedding3, _fn("project.module3.func3"))

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

    # The same function, re-parsed under a new node id with a new body.
    store_embedding("project", 1, embedding_v1, _fn("project.func"))
    store_embedding("project", 2, embedding_v2, _fn("project.func"))

    query = [0.0, 1.0] + [0.0] * 766
    results = search_embeddings(query, top_k=5)

    assert len(results) == 1
    assert results[0][0] == 2
    assert results[0][1] > 0.99


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_empty_search_returns_empty_list(integration_client: QdrantClient) -> None:
    from codebase_rag.vector_store import search_embeddings

    results = search_embeddings([0.5] * 768, top_k=5)
    assert results == []


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
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


def test_milvus_client_omits_unset_credentials() -> None:
    # Negative: an unset token or database must not reach the client at all,
    # so the client's own defaults stay in force.
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "MILVUS_URI", "http://localhost:19530"),
        patch.object(vs.settings, "MILVUS_TOKEN", None),
        patch.object(vs.settings, "MILVUS_DB_NAME", None),
    ):
        assert vs._milvus_client_kwargs() == {"uri": "http://localhost:19530"}


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


@pytest.mark.parametrize(
    ("lite_version", "expected"),
    [("3.0", True), ("3.0.1", True), ("3.0.0.post1", True), ("3.1.0", False)],
)
def test_milvus_lite_30_cosine_workaround_versions(
    lite_version: str, expected: bool
) -> None:
    import codebase_rag.vector_store as vs

    with (
        patch.object(vs.settings, "MILVUS_URI", "./milvus.db"),
        patch("codebase_rag.vector_store.version", return_value=lite_version),
    ):
        assert vs._uses_milvus_lite_30_cosine_distance() is expected


def _has_milvus_lite() -> bool:
    # A LOCAL Milvus uri needs the embedded milvus-lite server, which ships no
    # Windows wheels: pymilvus alone raises ConnectionConfigException there
    # (Windows CI).
    import importlib.util

    return importlib.util.find_spec("milvus_lite") is not None


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
@pytest.mark.skipif(not _has_milvus_lite(), reason="milvus-lite not installed")
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
        symbols = {
            101: _fn("pkg.auth.login"),
            102: _fn("pkg.billing.charge"),
            103: _fn("pkg.auth.refresh"),
            999: _fn("pkg.gone"),
        }
        stored = vs.store_embedding_batch(
            "pkg",
            [
                (101, [1.0, 0.0, 0.0, 0.0], symbols[101]),
                (102, [0.0, 1.0, 0.0, 0.0], symbols[102]),
                (103, [0.9, 0.1, 0.0, 0.0], symbols[103]),
            ],
        )
        vs.close_vector_store_client()
        results = vs.search_embeddings([0.95, 0.05, 0.0, 0.0], top_k=2)
        found_ids = vs.verify_stored_ids(
            "pkg", {n: symbols[n] for n in (101, 102, 999)}
        )
        vs.delete_project_embeddings("pkg", [101, 102])
        remaining_ids = vs.verify_stored_ids(
            "pkg", {n: symbols[n] for n in (101, 102, 103)}
        )

    assert stored == 3
    assert [node_id for node_id, _score in results] == [101, 103]
    assert found_ids == {101, 102}
    assert remaining_ids == {103}


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
@pytest.mark.skipif(not _has_milvus_lite(), reason="milvus-lite not installed")
def test_milvus_clean_rebuilds_a_collection_of_the_wrong_size(
    tmp_path: Path, reset_global_client: None
) -> None:
    import codebase_rag.vector_store as vs

    # A collection left by an embedder with a different output size must be
    # repairable with --clean; validating before the drop made that fail.
    with (
        patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.MILVUS),
        patch.object(vs.settings, "MILVUS_URI", str(tmp_path / "milvus.db")),
        patch.object(vs.settings, "MILVUS_COLLECTION_NAME", "code_embeddings_test"),
    ):
        # Left empty on purpose: milvus-lite indexes stored rows on a background
        # thread when it reopens the file, and dropping the collection while
        # that runs aborts the process on macOS.
        with patch.object(vs.settings, "MILVUS_VECTOR_DIM", 8):
            vs.get_milvus_client()
            vs.close_vector_store_client()
        with patch.object(vs.settings, "MILVUS_VECTOR_DIM", 4):
            with pytest.raises(ValueError, match="dimension 8"):
                vs.get_milvus_client()
            # The client that failed validation is not cached, so the check
            # runs again rather than being skipped.
            with pytest.raises(ValueError, match="dimension 8"):
                vs.get_milvus_client()
            vs.MilvusVectorStore().clear_all_embeddings()
            stored = vs.store_embedding_batch(
                "pkg", [(2, [1.0, 0.0, 0.0, 0.0], _fn("pkg.b"))]
            )
            found = vs.verify_stored_ids("pkg", {1: _fn("pkg.a"), 2: _fn("pkg.b")})

    assert stored == 1
    assert found == {2}


def test_a_failed_milvus_clean_does_not_leave_its_unvalidated_client_cached(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    instance = MagicMock()
    instance.has_collection.return_value = True
    instance.drop_collection.side_effect = RuntimeError("store is down")
    store = vs.MilvusVectorStore()
    with patch("codebase_rag.vector_store.MilvusClient", return_value=instance):
        with pytest.raises(RuntimeError):
            store.clear_all_embeddings()

    assert vs._CLIENT is None
    instance.close.assert_called_once()


@pytest.mark.skipif(not has_qdrant_client(), reason="qdrant-client not installed")
def test_clear_all_embeddings_qdrant_drops_and_recreates_collection(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    with patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client):
        vs.QdrantVectorStore().clear_all_embeddings()

    mock_client.delete_collection.assert_called_once_with(
        collection_name=vs.settings.QDRANT_COLLECTION_NAME
    )
    mock_client.create_collection.assert_called_once()


def test_clear_all_embeddings_milvus_drops_and_recreates_collection(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    with (
        patch("codebase_rag.vector_store.get_milvus_client", return_value=mock_client),
        patch("codebase_rag.vector_store._ensure_milvus_collection") as mock_ensure,
    ):
        vs.MilvusVectorStore().clear_all_embeddings()

    mock_client.drop_collection.assert_called_once_with(
        vs.settings.MILVUS_COLLECTION_NAME
    )
    mock_ensure.assert_called_once_with(mock_client)


def test_module_clear_all_embeddings_dispatches_to_backend() -> None:
    import codebase_rag.vector_store as vs

    store = MagicMock()
    with patch("codebase_rag.vector_store._get_vector_store", return_value=store):
        vs.clear_all_embeddings()

    store.clear_all_embeddings.assert_called_once_with()


def test_module_clear_all_embeddings_noop_without_backend() -> None:
    import codebase_rag.vector_store as vs

    with patch("codebase_rag.vector_store._get_vector_store", return_value=None):
        vs.clear_all_embeddings()


def test_clear_all_embeddings_qdrant_propagates_failure(
    reset_global_client: None,
) -> None:
    # A swallowed purge failure would let clean report success while stale
    # vectors keep resolving to unrelated nodes in the rebuilt graph.
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_client.delete_collection.side_effect = RuntimeError("qdrant down")
    with patch("codebase_rag.vector_store.get_qdrant_client", return_value=mock_client):
        store = vs.QdrantVectorStore()
        with pytest.raises(RuntimeError, match="qdrant down"):
            store.clear_all_embeddings()


def test_clear_all_embeddings_milvus_propagates_failure(
    reset_global_client: None,
) -> None:
    import codebase_rag.vector_store as vs

    mock_client = MagicMock()
    mock_client.drop_collection.side_effect = RuntimeError("milvus down")
    with patch("codebase_rag.vector_store.get_milvus_client", return_value=mock_client):
        store = vs.MilvusVectorStore()
        with pytest.raises(RuntimeError, match="milvus down"):
            store.clear_all_embeddings()


def test_process_exit_closes_local_client_cleanly(temp_qdrant_path: Path) -> None:
    # Nothing closes the module-global client on process exit, so teardown
    # falls to QdrantClient.__del__ during interpreter shutdown, where
    # qdrant's close() imports portalocker and dies with ImportError. Every
    # CLI run that touches the store then ends with an "Exception ignored"
    # traceback on stderr.
    if not has_qdrant_client():
        pytest.skip("qdrant-client not installed")

    script = (
        "from codebase_rag.vector_store import get_qdrant_client; get_qdrant_client()"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        env={
            **os.environ,
            # A QDRANT_URL would open a remote client and skip the local-path
            # shutdown this test exists to exercise. Dropping the key is not
            # enough: settings load with env_file=".env", so a checkout whose
            # .env sets QDRANT_URL still resolves it. Only an explicit empty
            # value overrides the file, and it is falsy, so the local path wins.
            "QDRANT_URL": "",
            "QDRANT_DB_PATH": str(temp_qdrant_path),
        },
        timeout=120,
    )
    assert result.returncode == 0
    assert "Exception ignored" not in result.stderr


# An embedding model whose output size differs from the collection's used to
# fail every batch with an opaque backend error that the batch wrapper
# swallowed, so a full index ran to the end and stored nothing.
@pytest.mark.parametrize(
    ("backend", "setting"),
    [
        (VectorStoreBackend.QDRANT, "QDRANT_VECTOR_DIM"),
        (VectorStoreBackend.MILVUS, "MILVUS_VECTOR_DIM"),
    ],
)
def test_storing_vectors_of_the_wrong_size_names_the_setting_to_change(
    backend: VectorStoreBackend, setting: str
) -> None:
    import codebase_rag.vector_store as vs

    store = MagicMock()
    store.backend = backend
    with (
        patch.object(vs.settings, setting, 4),
        patch.object(vs, "_get_vector_store", return_value=store),
    ):
        with pytest.raises(ValueError, match=f"3-dimensional.*{setting}"):
            vs.store_embedding_batch("pkg", [(1, [0.1, 0.2, 0.3], _fn("pkg.a"))])

    store.store_embedding_batch.assert_not_called()


def test_vectors_of_the_configured_size_reach_the_store() -> None:
    import codebase_rag.vector_store as vs

    store = MagicMock()
    store.backend = VectorStoreBackend.QDRANT
    store.store_embedding_batch.return_value = 1
    points = [(1, [0.1, 0.2, 0.3, 0.4], _fn("pkg.a"))]
    with (
        patch.object(vs.settings, "QDRANT_VECTOR_DIM", 4),
        patch.object(vs, "_get_vector_store", return_value=store),
    ):
        assert vs.store_embedding_batch("pkg", points) == 1

    store.store_embedding_batch.assert_called_once_with("pkg", points)


def test_a_query_of_the_wrong_size_names_the_setting_to_change() -> None:
    import codebase_rag.vector_store as vs

    store = MagicMock()
    store.backend = VectorStoreBackend.QDRANT
    with (
        patch.object(vs.settings, "QDRANT_VECTOR_DIM", 4),
        patch.object(vs, "_get_vector_store", return_value=store),
    ):
        with pytest.raises(ValueError, match="QDRANT_VECTOR_DIM"):
            vs.search_embeddings([0.1, 0.2])

    store.search_embeddings.assert_not_called()
