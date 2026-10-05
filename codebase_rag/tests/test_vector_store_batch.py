from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.types_defs import EmbeddingSymbol
from codebase_rag.utils.dependencies import has_qdrant_client

pytestmark = pytest.mark.skipif(
    not has_qdrant_client(), reason="qdrant-client not installed"
)

_PATCH_CLIENT = "codebase_rag.vector_store.get_qdrant_client"
_PATCH_SLEEP = "codebase_rag.vector_store.time.sleep"


def _fn(qualified_name: str) -> EmbeddingSymbol:
    return EmbeddingSymbol(cs.NodeLabel.FUNCTION, qualified_name)


def _method(qualified_name: str) -> EmbeddingSymbol:
    return EmbeddingSymbol(cs.NodeLabel.METHOD, qualified_name)


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
            (1, [0.1] * 768, _fn("mod.func1")),
            (2, [0.2] * 768, _fn("mod.func2")),
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
            result = store_embedding_batch("mod", [(1, [0.1] * 768, _fn("mod.func"))])

        assert result == 0

    def test_builds_correct_point_structs(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, store_embedding_batch

        mock_client = MagicMock()
        embedding = [0.5] * 768
        points = [(42, embedding, _fn("pkg.module.fn"))]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            store_embedding_batch("pkg", points)

        call_kwargs = mock_client.upsert.call_args[1]
        stored_points = call_kwargs["points"]
        assert len(stored_points) == 1
        assert stored_points[0].id == embedding_point_id("pkg", _fn("pkg.module.fn"))
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

        point_id = embedding_point_id("pkg", _fn("pkg.module.fn"))

        assert str(uuid.UUID(point_id)) == point_id
        assert embedding_point_id("pkg", _fn("pkg.module.fn")) == point_id

    def test_is_the_same_in_every_process(self) -> None:
        # Pinned: a sync in a later process must find the point an earlier
        # one stored, so the derivation may not depend on anything but the
        # symbol (no hash seed, no enum identity).
        from codebase_rag.vector_store import embedding_point_id

        point_id = embedding_point_id("pkg", _fn("pkg.module.fn"))

        assert point_id == "e6ddcec0-8f81-594f-ae7d-daf4ff752ea7"

    def test_differs_by_label(self) -> None:
        # A Function and a Method may share a qualified name (C++ spells
        # `Clock::now` both ways across an `#if`); neither may overwrite the
        # other's point (bot review).
        from codebase_rag.vector_store import embedding_point_id

        point_id = embedding_point_id("pkg", _fn("pkg.m.Clock.now"))

        assert embedding_point_id("pkg", _method("pkg.m.Clock.now")) != point_id

    def test_differs_by_name_and_by_project(self) -> None:
        # Negative: neither another function nor another project's function
        # of the same name may overwrite this one's point.
        from codebase_rag.vector_store import embedding_point_id

        point_id = embedding_point_id("pkg", _fn("pkg.module.fn"))

        assert embedding_point_id("pkg", _fn("pkg.module.other")) != point_id
        assert embedding_point_id("pkg2", _fn("pkg.module.fn")) != point_id
        assert embedding_point_id("pk", _fn("g.pkg.module.fn")) != point_id


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
            deleted = delete_project_embeddings("myproject", [1, 2])

        mock_client.delete.assert_not_called()
        # The project's points could not be found, so they may still be there.
        assert deleted is False

    def test_reports_a_failed_delete(self) -> None:
        # Review of PR 2497: retiring a project must know its vectors are
        # still there, or it deletes the node ids they are keyed by.
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        _scrolling(mock_client, [], [])
        mock_client.delete.side_effect = Exception("connection lost")

        with patch(_PATCH_CLIENT, return_value=mock_client):
            deleted = delete_project_embeddings("myproject", [1, 2])

        assert deleted is False

    def test_reports_a_successful_delete(self) -> None:
        # Negative.
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        _scrolling(mock_client, [], [])

        with patch(_PATCH_CLIENT, return_value=mock_client):
            deleted = delete_project_embeddings("myproject", [1, 2])

        assert deleted is True

    def test_reports_nothing_to_delete_as_done(self) -> None:
        from codebase_rag.vector_store import delete_project_embeddings

        mock_client = MagicMock()
        _scrolling(mock_client, [], [])

        with patch(_PATCH_CLIENT, return_value=mock_client):
            deleted = delete_project_embeddings("myproject", [])

        assert deleted is True

    def test_reports_no_vector_store_as_done(self) -> None:
        # A run without a vector store wrote no vectors to delete.
        import codebase_rag.vector_store as vs

        with patch.object(vs, "_get_vector_store", return_value=None):
            deleted = vs.delete_project_embeddings("myproject", [1, 2])

        assert deleted is True


class TestDeleteStaleEmbeddings:
    def test_deletes_what_no_longer_matches_the_graph(self) -> None:
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        current = {1: _fn("p.m.kept"), 2: _fn("p.m.moved")}
        kept = _point(embedding_point_id("p", _fn("p.m.kept")), node_id=1)
        # Re-created under node 2; the point still names the node it left.
        moved = _point(embedding_point_id("p", _fn("p.m.moved")), node_id=9)
        gone = _point(embedding_point_id("p", _fn("p.m.gone")), node_id=3)
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
            [_point(embedding_point_id("p", _fn("p.m.kept")), node_id=1)],
            [_point(8, qualified_name="q.m.f")],
        )

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", {1: _fn("p.m.kept")})

        mock_client.delete.assert_not_called()
        assert removed == 0

    def test_keeps_a_function_and_a_method_that_share_a_name(self) -> None:
        # Negative: two live nodes, two points, nothing stale.
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        function, method = _fn("p.m.C.f"), _method("p.m.C.f")
        mock_client = MagicMock()
        _scrolling(
            mock_client,
            [
                _point(embedding_point_id("p", function), node_id=1),
                _point(embedding_point_id("p", method), node_id=2),
            ],
            [],
        )

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", {1: function, 2: method})

        mock_client.delete.assert_not_called()
        assert removed == 0

    def test_drops_the_method_the_graph_lost_beside_its_namesake(self) -> None:
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        function, method = _fn("p.m.C.f"), _method("p.m.C.f")
        lost = _point(embedding_point_id("p", method), node_id=2)
        mock_client = MagicMock()
        _scrolling(
            mock_client,
            [_point(embedding_point_id("p", function), node_id=1), lost],
            [],
        )

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", {1: function})

        assert mock_client.delete.call_args[1]["points_selector"] == [lost.id]
        assert removed == 1

    def test_drops_a_point_that_names_no_node(self) -> None:
        # A point that names no node, under a key the graph no longer
        # expects: two missing node ids must not compare as a match.
        from codebase_rag.vector_store import (
            delete_stale_embeddings,
            embedding_point_id,
        )

        orphan = _point(embedding_point_id("p", _fn("p.m.gone")))
        mock_client = MagicMock()
        _scrolling(mock_client, [orphan], [])

        with patch(_PATCH_CLIENT, return_value=mock_client):
            removed = delete_stale_embeddings("p", {})

        assert mock_client.delete.call_args[1]["points_selector"] == [orphan.id]
        assert removed == 1


class TestVerifyStoredIds:
    def test_returns_found_ids(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = [
            _point(embedding_point_id("p", _fn("p.a")), node_id=1),
            _point(embedding_point_id("p", _fn("p.c")), node_id=3),
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids(
                "p", {1: _fn("p.a"), 2: _fn("p.b"), 3: _fn("p.c")}
            )

        assert result == {1, 3}

    def test_a_point_that_names_another_node_is_not_found(self) -> None:
        # The symbol's point still names the node it had before a re-parse.
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = [
            _point(embedding_point_id("p", _fn("p.a")), node_id=1)
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids("p", {5: _fn("p.a")})

        assert result == set()

    def test_finds_a_function_and_a_method_that_share_a_name(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        function, method = _fn("p.m.C.f"), _method("p.m.C.f")
        mock_client = MagicMock()
        mock_client.retrieve.return_value = [
            _point(embedding_point_id("p", function), node_id=1),
            _point(embedding_point_id("p", method), node_id=2),
        ]

        with patch(_PATCH_CLIENT, return_value=mock_client):
            result = verify_stored_ids("p", {1: function, 2: method})

        assert result == {1, 2}

    def test_returns_empty_for_empty_input(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        result = verify_stored_ids("p", {})
        assert result == set()

    def test_raises_on_exception(self) -> None:
        from codebase_rag.vector_store import verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.side_effect = Exception("fail")
        ids = {1: _fn("p.a"), 2: _fn("p.b")}

        with (
            patch(_PATCH_CLIENT, return_value=mock_client),
            pytest.raises(Exception, match="fail"),
        ):
            verify_stored_ids("p", ids)

    def test_batches_large_id_sets(self) -> None:
        from codebase_rag.vector_store import _RETRIEVE_BATCH_SIZE, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        expected = {i: _fn(f"p.f{i}") for i in range(_RETRIEVE_BATCH_SIZE + 100)}

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids("p", expected)

        assert mock_client.retrieve.call_count == 2

    def test_retrieve_called_with_correct_params(self) -> None:
        from codebase_rag.vector_store import embedding_point_id, verify_stored_ids

        mock_client = MagicMock()
        mock_client.retrieve.return_value = []

        with patch(_PATCH_CLIENT, return_value=mock_client):
            verify_stored_ids("p", {10: _fn("p.a"), 20: _fn("p.b")})

        call_kwargs = mock_client.retrieve.call_args[1]
        assert call_kwargs["with_payload"] == ["node_id"]
        assert call_kwargs["with_vectors"] is False
        assert set(call_kwargs["ids"]) == {
            embedding_point_id("p", _fn("p.a")),
            embedding_point_id("p", _fn("p.b")),
        }
