from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import VectorStoreBackend
from codebase_rag.utils.dependencies import has_qdrant_client

pytestmark = pytest.mark.skipif(
    not has_qdrant_client(), reason="qdrant-client not installed"
)

_PATCH_CLIENT = "codebase_rag.vector_store.get_qdrant_client"
_PATCH_SLEEP = "codebase_rag.vector_store.time.sleep"


@pytest.fixture(autouse=True)
def use_qdrant_backend() -> Generator[None, None, None]:
    import codebase_rag.vector_store as vs

    with patch.object(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT):
        yield


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

        points = [MagicMock()]
        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP),
            pytest.raises(ConnectionError, match="timeout"),
        ):
            _upsert_with_retry(points)

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
            result = store_embedding_batch("mod", points)

        assert result == 2

    def test_returns_zero_on_empty(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        result = store_embedding_batch("mod", [])
        assert result == 0

    def test_returns_zero_on_failure(self) -> None:
        from codebase_rag.vector_store import store_embedding_batch

        mock_client = MagicMock()
        mock_client.upsert.side_effect = Exception("fail")

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            patch(_PATCH_SLEEP),
        ):
            result = store_embedding_batch("mod", [(1, [0.1] * 768, "mod.func")])

        assert result == 0

    def test_builds_correct_point_structs(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, store_embedding_batch

        mock_client = MagicMock()
        embedding = [0.5] * 768
        points = [(42, embedding, "pkg.module.fn")]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            store_embedding_batch("pkg", points)

        call_kwargs = mock_client.upsert.call_args[1]
        stored_points = call_kwargs["points"]
        assert len(stored_points) == 1
        assert stored_points[0].id == embedding_point_id("pkg", "pkg.module.fn")
        assert stored_points[0].vector == embedding
        assert stored_points[0].payload == {
            "node_id": 42,
            "qualified_name": "pkg.module.fn",
            "project": "pkg",
        }


class TestEmbeddingPointId:
    def test_is_a_uuid_fixed_by_project_and_name(self) -> None:
        import uuid

        from codebase_rag.vector_store import embedding_point_id

        point_id = embedding_point_id("pkg", "pkg.module.fn")

        assert str(uuid.UUID(point_id)) == point_id
        assert embedding_point_id("pkg", "pkg.module.fn") == point_id

    def test_differs_by_name_and_by_project(self) -> None:
        # Negative: neither another function nor another project's function
        # of the same name may overwrite this one's point.
        from codebase_rag.vector_store import embedding_point_id

        point_id = embedding_point_id("pkg", "pkg.module.fn")

        assert embedding_point_id("pkg", "pkg.module.other") != point_id
        assert embedding_point_id("pkg2", "pkg.module.fn") != point_id
        assert embedding_point_id("pk", "g.pkg.module.fn") != point_id


def _scrolling(mock_client: MagicMock, *pages: list[MagicMock]) -> None:
    # Each scroll answers one page and no further offset.
    mock_client.scroll.side_effect = [(page, None) for page in pages]


def _point(point_id: int | str, **payload: int | str) -> MagicMock:
    point = MagicMock()
    point.id = point_id
    point.payload = payload
    return point


class TestDeleteProjectEmbeddings:
    def test_deletes_given_ids_and_the_projects_stored_points(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        # The project's points keyed by symbol, then the unowned ones.
        _scrolling(
            mock_client,
            [_point("a-uuid", node_id=9)],
            [
                _point(7, qualified_name="myproject.m.f"),
                _point(8, qualified_name="x.f"),
            ],
        )
        node_ids = [1, 2, 3]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", node_ids)

        mock_client.delete.assert_called_once()
        call_kwargs = mock_client.delete.call_args[1]
        assert call_kwargs["points_selector"] == [1, 2, 3, "a-uuid", 7]

    def test_noop_when_nothing_is_stored(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        _scrolling(mock_client, [], [])

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", [])

        mock_client.delete.assert_not_called()

    def test_handles_exception_gracefully(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        _scrolling(mock_client, [], [])
        mock_client.delete.side_effect = Exception("connection lost")

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", [1, 2])

    def test_handles_a_failed_lookup_gracefully(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        mock_client.scroll.side_effect = Exception("connection lost")

        with patch(_PATCH_CLIENT, return_value=mock_client):
            delete_project_embeddings("myproject", [1, 2])

        mock_client.delete.assert_not_called()


class TestDeleteStaleEmbeddings:
    def test_deletes_what_no_longer_matches_the_graph(self) -> None:
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        current = {1: "p.m.kept", 2: "p.m.moved"}
        kept = _point(embedding_point_id("p", "p.m.kept"), node_id=1)
        # Re-created under node 2; the point still names the node it left.
        moved = _point(embedding_point_id("p", "p.m.moved"), node_id=9)
        gone = _point(embedding_point_id("p", "p.m.gone"), node_id=3)
        mock_client = MagicMock()
        _scrolling(
            mock_client,
            [kept, moved, gone],
            [_point(7, qualified_name="p.m.kept"), _point(8, qualified_name="q.m.f")],
        )

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", current)

        call_kwargs = mock_client.delete.call_args[1]
        assert call_kwargs["points_selector"] == [moved.id, gone.id, 7]
        # The node-id keyed point is the one-off move to the new keys, not
        # drift, so it is logged apart rather than counted.
        assert removed == 2

    def test_deletes_nothing_when_everything_matches(self) -> None:
        # Negative: a sync that changed nothing deletes nothing.
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        mock_client = MagicMock()
        _scrolling(
            mock_client,
            [_point(embedding_point_id("p", "p.m.kept"), node_id=1)],
            [_point(8, qualified_name="q.m.f")],
        )

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", {1: "p.m.kept"})

        mock_client.delete.assert_not_called()
        assert removed == 0


class TestVerifyStoredIds:
    def test_returns_found_ids(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = [
            _point(embedding_point_id("p", "p.a"), node_id=1),
            _point(embedding_point_id("p", "p.c"), node_id=3),
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids("p", {1: "p.a", 2: "p.b", 3: "p.c"})

        assert result == {1, 3}

    def test_a_point_that_names_another_node_is_not_found(self) -> None:
        # The symbol's point still names the node it had before a re-parse.
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = [
            _point(embedding_point_id("p", "p.a"), node_id=1)
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids("p", {5: "p.a"})

        assert result == set()

    def test_returns_empty_for_empty_input(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        result = verify_stored_ids("p", {})
        assert result == set()

    def test_raises_on_exception(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.side_effect = Exception("fail")

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            pytest.raises(Exception, match="fail"),
        ):
            verify_stored_ids("p", {1: "p.a", 2: "p.b"})

    def test_batches_large_id_sets(self) -> None:
        from codebase_rag.vector_store import _RETRIEVE_BATCH_SIZE, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        expected = {i: f"p.f{i}" for i in range(_RETRIEVE_BATCH_SIZE + 100)}

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids("p", expected)

        assert mock_client.retrieve.call_count == 2

    def test_retrieve_called_with_correct_params(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids("p", {10: "p.a", 20: "p.b"})

        call_kwargs = mock_client.retrieve.call_args[1]
        assert call_kwargs["with_payload"] == ["node_id"]
        assert call_kwargs["with_vectors"] is False
        assert set(call_kwargs["ids"]) == {
            embedding_point_id("p", "p.a"),
            embedding_point_id("p", "p.b"),
        }
