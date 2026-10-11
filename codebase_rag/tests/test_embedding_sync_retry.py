"""A sync that could not store an embedding must not pass for a finished one.

Points are keyed by symbol since issue #2447, and each names the node it was
stored for. A re-parse re-creates a file's functions under new node ids, so
the sync's stale-point cleanup deletes the point of an edited function: it
names a node the graph no longer holds. When the vector store refused that
function's replacement, the function was left with no point at all, yet the
sync still published the edited file's hash, and every later sync of the
unchanged checkout took the in-sync fast path, which never embeds. Semantic
search kept missing the function until some unrelated file changed.

These tests run whole syncs through the real parsers into the eval graph
double, against an embedded Qdrant, with the embeddings query answered from a
script the way a re-parse changes it: a re-parsed file's functions come back
under new node ids, a deleted function does not come back at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import vector_store as vs
from codebase_rag.checkout_state import state_file
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.utils.dependencies import has_pymilvus, has_qdrant_client
from evals.cgr_graph import _StatefulIngestor

pytestmark = pytest.mark.skipif(
    not has_qdrant_client(), reason="qdrant-client not installed"
)

_DIM = 8
_PROJECT = "svc"
_MODULE = "m.py"

_SOURCE_V1 = "def keep_me():\n    return 1\n\ndef delete_me():\n    return 2\n"
_SOURCE_KEEP_ONLY = "def keep_me():\n    return 1\n"
_SOURCE_KEEP_EDITED = "def keep_me():\n    return 11\n"
_SPANS = {"keep_me": (1, 2), "delete_me": (4, 5)}

_KEEP = f"{_PROJECT}.m.keep_me"
_DELETE = f"{_PROJECT}.m.delete_me"

type Embed = Callable[..., list[list[float]]]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255 + 0.01 for b in digest[:_DIM]]


def _fake_embed_batch(snippets: list[str], **_: bool | int | str) -> list[list[float]]:
    return [_vector(s) for s in snippets]


def _model_unavailable(snippets: list[str], **_: bool | int | str) -> list[list[float]]:
    raise RuntimeError("model unavailable")


def _row(node_id: int, qualified_name: str, path: str = _MODULE) -> ResultRow:
    start, end = _SPANS[qualified_name.rsplit(".", 1)[1]]
    return {
        cs.KEY_NODE_ID: node_id,
        cs.KEY_QUALIFIED_NAME: qualified_name,
        cs.KEY_LABEL: cs.NodeLabel.FUNCTION,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
        cs.KEY_PATH: path,
    }


class _Graph(_StatefulIngestor):
    """The eval graph double, answering the embeddings query from `rows`.

    The double holds no internal node ids, which the embeddings query reads,
    so each test scripts that answer for the sync it runs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[ResultRow] = []

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        if query == cs.CYPHER_QUERY_EMBEDDINGS:
            return list(self.rows)
        return super().fetch_all(query, params)


@pytest.fixture(autouse=True)
def small_vectors(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setattr(vs.settings, "QDRANT_VECTOR_DIM", _DIM)
    monkeypatch.setattr(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT)
    vs.close_vector_store_client()
    yield
    vs.close_vector_store_client()


@pytest.fixture
def repo(temp_repo: Path) -> Path:
    (temp_repo / _MODULE).write_text(_SOURCE_V1)
    return temp_repo


@pytest.fixture
def graph() -> _Graph:
    return _Graph()


def _updater(repo: Path, graph: _Graph, skip_embeddings: bool = False) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=graph,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=_PROJECT,
        skip_embeddings=skip_embeddings,
    )


@contextmanager
def _semantic_extra() -> Iterator[None]:
    # The embedding model is faked, so the run need not have torch to embed.
    with patch(
        "codebase_rag.graph_updater.has_semantic_dependencies", return_value=True
    ):
        yield


def _sync(
    repo: Path,
    graph: _Graph,
    functions: Mapping[int, str] | list[ResultRow],
    *,
    source: str | None = None,
    embed: Embed = _fake_embed_batch,
    skip_embeddings: bool = False,
) -> GraphUpdater:
    """One whole sync, the graph's functions by node id as `functions` says."""
    if source is not None:
        (repo / _MODULE).write_text(source)
    graph.rows = (
        [_row(node_id, qn) for node_id, qn in functions.items()]
        if isinstance(functions, Mapping)
        else list(functions)
    )
    updater = _updater(repo, graph, skip_embeddings)
    with (
        _semantic_extra(),
        patch("codebase_rag.embedder.embed_code_batch", side_effect=embed),
    ):
        updater.run()
    return updater


def _in_sync(repo: Path, graph: _Graph, skip_embeddings: bool = False) -> bool:
    """What the next sync's fast-path check answers, before it runs."""
    with _semantic_extra():
        return _updater(repo, graph, skip_embeddings)._is_already_in_sync()


@contextmanager
def _store_down() -> Iterator[None]:
    # Every attempt fails, as when the vector store is unreachable.
    with patch.object(vs, "_upsert_with_retry", side_effect=RuntimeError("down")):
        yield


def _points() -> list[tuple[str, int]]:
    """Every point in the collection, as (qualified name, node id)."""
    client = vs.get_qdrant_client()
    try:
        points, _ = client.scroll(
            collection_name=vs.settings.QDRANT_COLLECTION_NAME,
            limit=1000,
            with_payload=True,
        )
    finally:
        vs.close_vector_store_client()
    return sorted(
        (p.payload[cs.PAYLOAD_QUALIFIED_NAME], p.payload[cs.PAYLOAD_NODE_ID])
        for p in points
        if p.payload is not None
    )


class TestAMissingEmbeddingIsRetried:
    def test_an_unchanged_sync_re_embeds_a_function_whose_upsert_failed(
        self, repo: Path, graph: _Graph
    ) -> None:
        # The review's reproduction: keep_me is edited, its node re-created
        # as 2, and the store refuses the new point. The old one names node
        # 1, which the graph no longer holds, so the cleanup deletes it.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        assert _points() == [(_KEEP, 1)]
        with _store_down():
            _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
        assert _points() == []

        retry = _sync(repo, graph, {2: _KEEP})

        assert retry.skipped_because_in_sync is False
        assert _points() == [(_KEEP, 2)]

    @pytest.mark.parametrize("failure", ["upsert", "embedding"])
    def test_a_sync_that_left_an_embedding_missing_is_not_in_sync(
        self, repo: Path, graph: _Graph, failure: str
    ) -> None:
        # The edited file's hash is published either way; what must not
        # follow is the next unchanged sync reading it as fully synced.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        if failure == "upsert":
            with _store_down():
                _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
        else:
            _sync(
                repo,
                graph,
                {2: _KEEP},
                source=_SOURCE_KEEP_EDITED,
                embed=_model_unavailable,
            )

        assert _in_sync(repo, graph) is False

    def test_a_stale_point_whose_delete_failed_is_removed_by_the_next_sync(
        self, repo: Path, graph: _Graph
    ) -> None:
        # The cleanup is owed the same way: an unchanged checkout would
        # otherwise keep a deleted function's point until some file changed.
        _sync(repo, graph, {1: _KEEP, 2: _DELETE})
        with patch.object(
            vs.QdrantVectorStore,
            "delete_stale_embeddings",
            side_effect=RuntimeError("down"),
        ):
            _sync(repo, graph, {3: _KEEP}, source=_SOURCE_KEEP_ONLY)
        assert _points() == [(_DELETE, 2), (_KEEP, 3)]

        retry = _sync(repo, graph, {3: _KEEP})

        assert retry.skipped_because_in_sync is False
        assert _points() == [(_KEEP, 3)]

    def test_a_pass_that_failed_outright_is_retried(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Nothing tells what such a pass stored, so the next sync runs it again.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        with patch.object(
            vs.QdrantVectorStore,
            "verify_stored_ids",
            side_effect=RuntimeError("down"),
        ):
            _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)

        assert _in_sync(repo, graph) is False

    def test_the_retry_settles_what_was_owed(self, repo: Path, graph: _Graph) -> None:
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        with _store_down():
            _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
        assert _sync(repo, graph, {2: _KEEP}).skipped_because_in_sync is False

        assert _in_sync(repo, graph) is True
        assert not state_file(repo, cs.EMBEDDINGS_PENDING_FILENAME).exists()


class TestWhatTheRetryLeavesAlone:
    def test_a_stored_replacement_still_replaces_the_stale_point(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: the edit's new point lands, the old one goes, and the
        # unchanged checkout is back on the fast path at once.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)

        _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)

        assert _points() == [(_KEEP, 2)]
        assert _in_sync(repo, graph) is True

    def test_a_deleted_function_loses_its_point_while_another_upsert_fails(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: owing keep_me's point does not keep delete_me's, whose
        # symbol is gone from the graph.
        _sync(repo, graph, {1: _KEEP, 2: _DELETE})
        with _store_down():
            _sync(repo, graph, {3: _KEEP}, source=_SOURCE_KEEP_ONLY)

        assert _DELETE not in {qn for qn, _node in _points()}

    def test_a_function_whose_source_cannot_be_read_owes_nothing(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: no retry could embed it, so it must not hold every later
        # sync off the fast path.
        unreadable = _row(2, _DELETE, path="gone.py")

        _sync(repo, graph, [_row(1, _KEEP), unreadable], source=_SOURCE_KEEP_ONLY)

        assert _points() == [(_KEEP, 1)]
        assert _in_sync(repo, graph) is True

    def test_a_sync_without_embeddings_neither_owes_nor_writes(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: --skip-embeddings leaves the store and the fast path as
        # they were, even across an edit it re-parses.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)

        _sync(
            repo,
            graph,
            {2: _KEEP},
            source=_SOURCE_KEEP_EDITED,
            skip_embeddings=True,
        )

        assert _points() == [(_KEEP, 1)]
        assert _in_sync(repo, graph, skip_embeddings=True) is True

    def test_a_sync_without_embeddings_leaves_an_owed_retry_to_one_with_them(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: a run that cannot embed cannot pay the debt, so it keeps
        # the fast path rather than re-indexing for nothing.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        with _store_down():
            _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)

        skipped = _sync(repo, graph, {2: _KEEP}, skip_embeddings=True)

        assert skipped.skipped_because_in_sync is True
        assert _points() == []
        retry = _sync(repo, graph, {2: _KEEP})
        assert retry.skipped_because_in_sync is False
        assert _points() == [(_KEEP, 2)]


def _write_legacy(points: Mapping[int, str]) -> None:
    """Points as the store wrote them before issue #2447: keyed by node id."""
    from qdrant_client.models import PointStruct

    client = vs.get_qdrant_client()
    try:
        client.upsert(
            collection_name=vs.settings.QDRANT_COLLECTION_NAME,
            points=[
                PointStruct(
                    id=node_id,
                    vector=_vector(qn),
                    payload={
                        cs.PAYLOAD_NODE_ID: node_id,
                        cs.PAYLOAD_QUALIFIED_NAME: qn,
                    },
                )
                for node_id, qn in points.items()
            ],
        )
    finally:
        vs.close_vector_store_client()


def _point_ids() -> list[int | str]:
    client = vs.get_qdrant_client()
    try:
        points, _ = client.scroll(
            collection_name=vs.settings.QDRANT_COLLECTION_NAME, limit=1000
        )
    finally:
        vs.close_vector_store_client()
    return sorted((p.id for p in points), key=str)


class TestTheMoveFromNodeIdKeys:
    """A collection written before issue #2447 holds points keyed by node id."""

    def test_a_sync_still_moves_the_projects_points_to_the_new_keys(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: two copies of keep_me and the long-deleted delete_me give
        # way to one point under the symbol's key, and nothing is owed.
        _write_legacy({1: _KEEP, 2: _DELETE, 5: _KEEP})

        _sync(repo, graph, {9: _KEEP}, source=_SOURCE_KEEP_ONLY)

        assert _points() == [(_KEEP, 9)]
        assert all(isinstance(point_id, str) for point_id in _point_ids())
        assert _in_sync(repo, graph) is True

    def test_a_legacy_point_whose_replacement_failed_moves_on_the_retry(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: it still names keep_me's current node, so it stays until
        # the new point is in; the retry is what moves it.
        _write_legacy({1: _KEEP})
        with _store_down():
            _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        assert _point_ids() == [1]

        _sync(repo, graph, {1: _KEEP})

        assert _points() == [(_KEEP, 1)]
        assert all(isinstance(point_id, str) for point_id in _point_ids())


def _has_milvus_lite() -> bool:
    import importlib.util

    return importlib.util.find_spec("milvus_lite") is not None


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
@pytest.mark.skipif(not _has_milvus_lite(), reason="milvus-lite not installed")
def test_milvus_retries_a_row_whose_upsert_failed(
    repo: Path, graph: _Graph, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Milvus keys rows by node id, so the edited function's old row is just
    # as stale, and its retry is owed the same way.
    monkeypatch.setattr(vs.settings, "MILVUS_VECTOR_DIM", _DIM)
    monkeypatch.setattr(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.MILVUS)
    monkeypatch.setattr(vs.settings, "MILVUS_URI", str(tmp_path / "milvus.db"))
    monkeypatch.setattr(vs.settings, "MILVUS_CONSISTENCY_LEVEL", "Strong")

    def rows() -> list[tuple[str, int]]:
        found = vs.get_milvus_client().query(
            collection_name=vs.settings.MILVUS_COLLECTION_NAME,
            filter=f"{cs.PAYLOAD_NODE_ID} >= 0",
            output_fields=[cs.PAYLOAD_NODE_ID, cs.PAYLOAD_QUALIFIED_NAME],
        )
        return sorted(
            (row[cs.PAYLOAD_QUALIFIED_NAME], row[cs.PAYLOAD_NODE_ID]) for row in found
        )

    _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
    with patch.object(vs, "_store_batch", return_value=0):
        _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
    assert rows() == []

    retry = _sync(repo, graph, {2: _KEEP})

    assert retry.skipped_because_in_sync is False
    assert rows() == [(_KEEP, 2)]
