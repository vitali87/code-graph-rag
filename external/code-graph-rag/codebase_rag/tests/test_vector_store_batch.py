from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.utils.dependencies import has_qdrant_client

pytestmark = pytest.mark.skipif(
    not has_qdrant_client(), reason="qdrant-client not installed"
)

_PATCH_CLIENT = "codebase_rag.vector_store.get_qdrant_client"
_PATCH_SLEEP = "codebase_rag.vector_store.time.sleep"


class TestUpsertWithRetry:
    def test_succeeds_on_first_attempt(self) -> None:
        from codebase_rag.vector_store import _upsert_with_retry

        mock_client = MagicMock()
        mock_point = MagicMock()

        with patch(_PATCH_CLIENT, return_value=mock_client):
            _upsert_with_retry([mock_point])

        mock_client.upsert.assert_called_once()

    def test_retries_on_failure_then_succeeds(self) -> None:
        from codebase_rag.vector_store import _upsert_with_retry

        mock_client = MagicMock()
        mock_client.upsert.side_effect = [
            ConnectionError("timeout"),
            None,
        ]

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP) as mock_sleep,
        ):
            _upsert_with_retry([MagicMock()])

        assert mock_client.upsert.call_count == 2
        mock_sleep.assert_called_once()

    def test_raises_after_exhausting_retries(self) -> None:
        from codebase_rag.vector_store import _upsert_with_retry

        mock_client = MagicMock()
        mock_client.upsert.side_effect = ConnectionError("timeout")

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP),
            pytest.raises(ConnectionError, match="timeout"),
        ):
            _upsert_with_retry([MagicMock()])

    def test_exponential_backoff_delays(self) -> None:
        from codebase_rag.vector_store import _upsert_with_retry

        mock_client = MagicMock()
        mock_client.upsert.side_effect = [
            ConnectionError("fail"),
            ConnectionError("fail"),
            None,
        ]

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP) as mock_sleep,
        ):
            _upsert_with_retry([MagicMock()])

        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[1] > delays[0]


class TestStoreEmbeddingBatch:
    def test_returns_count_on_success(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        mock_client = MagicMock()
        points = [
            (1, [0.1] * 768, "mod.func1"),
            (2, [0.2] * 768, "mod.func2"),
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = store_embedding_batch(points)

        assert result == 2

    def test_returns_zero_on_empty(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        result = store_embedding_batch([])
        assert result == 0

    def test_returns_zero_on_failure(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        mock_client = MagicMock()
        mock_client.upsert.side_effect = Exception("fail")

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP),
        ):
            result = store_embedding_batch([(1, [0.1] * 768, "mod.func")])

        assert result == 0

    def test_builds_correct_point_structs(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        mock_client = MagicMock()
        embedding = [0.5] * 768
        points = [(42, embedding, "pkg.module.fn")]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            store_embedding_batch(points)

        call_kwargs = mock_client.upsert.call_args[1]
        stored_points = call_kwargs["points"]
        assert len(stored_points) == 1
        assert stored_points[0].id == 42
        assert stored_points[0].vector == embedding
        assert stored_points[0].payload["node_id"] == 42
        assert stored_points[0].payload["qualified_name"] == "pkg.module.fn"


class TestDeleteProjectEmbeddings:
    def test_deletes_given_ids(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        node_ids = [1, 2, 3]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", node_ids)

        mock_client.delete.assert_called_once()
        call_kwargs = mock_client.delete.call_args[1]
        assert call_kwargs["points_selector"] == [1, 2, 3]

    def test_noop_on_empty_ids(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", [])

        mock_client.delete.assert_not_called()

    def test_handles_exception_gracefully(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("connection lost")

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", [1, 2])


class TestVerifyStoredIds:
    def test_returns_found_ids(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        mock_client = MagicMock()
        mock_point_1 = MagicMock()
        mock_point_1.id = 1
        mock_point_2 = MagicMock()
        mock_point_2.id = 3
        mock_client.retrieve.return_value = [mock_point_1, mock_point_2]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids({1, 2, 3})

        assert result == {1, 3}

    def test_returns_empty_for_empty_input(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        result = verify_stored_ids(set())
        assert result == set()

    def test_raises_on_exception(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.side_effect = Exception("fail")

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            pytest.raises(Exception, match="fail"),
        ):
            verify_stored_ids({1, 2})

    def test_batches_large_id_sets(self) -> None:
        from codebase_rag.vector_store import _RETRIEVE_BATCH_SIZE, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        large_id_set = set(range(_RETRIEVE_BATCH_SIZE + 100))

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids(large_id_set)

        assert mock_client.retrieve.call_count == 2

    def test_retrieve_called_with_correct_params(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids({10, 20})

        call_kwargs = mock_client.retrieve.call_args[1]
        assert call_kwargs["with_payload"] is False
        assert call_kwargs["with_vectors"] is False
        assert set(call_kwargs["ids"]) == {10, 20}
