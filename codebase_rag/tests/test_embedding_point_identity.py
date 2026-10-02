"""One vector per function or method, whatever the graph did to its node.

Embeddings were keyed by Memgraph's internal node id. An incremental sync
deletes and re-creates a re-parsed file's nodes under new ids, so every edit
added another point for each of the file's functions, and the points of
deleted functions stayed forever: semantic search returned several copies of
each edited function plus functions long gone (issue #2447).

These tests drive the sync's embedding pass against a real embedded Qdrant
(and Milvus Lite), with the graph's answer for the project scripted per sync
the way an incremental re-parse changes it: a re-parsed file's functions come
back under new node ids, a deleted function does not come back at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import vector_store as vs
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import ResultRow
from codebase_rag.utils.dependencies import has_pymilvus, has_qdrant_client

pytestmark = pytest.mark.skipif(
    not has_qdrant_client(), reason="qdrant-client not installed"
)

_DIM = 8
_PROJECT = "svc"
_OTHER = "other"
_MODULE = "m.py"

# Line spans of each function in the module source below.
_SOURCE_V1 = "def keep_me():\n    return 1\n\ndef delete_me():\n    return 2\n"
_SOURCE_V2 = "def keep_me():\n    return 1\n"
_SOURCE_V3 = "def keep_me():\n    return 11\n"
_SPANS = {"keep_me": (1, 2), "delete_me": (4, 5)}

_KEEP = f"{_PROJECT}.m.keep_me"
_DELETE = f"{_PROJECT}.m.delete_me"


def _vector(text: str) -> list[float]:
    # Deterministic and content-dependent, so an edit changes the vector.
    digest = hashlib.sha256(text.encode()).digest()
    return [b / 255 + 0.01 for b in digest[:_DIM]]


def _fake_embed_batch(snippets: list[str], **_: bool | int | str) -> list[list[float]]:
    return [_vector(s) for s in snippets]


def _row(node_id: int, qualified_name: str) -> ResultRow:
    start, end = _SPANS[qualified_name.rsplit(".", 1)[1]]
    return {
        cs.KEY_NODE_ID: node_id,
        cs.KEY_QUALIFIED_NAME: qualified_name,
        cs.KEY_START_LINE: start,
        cs.KEY_END_LINE: end,
        cs.KEY_PATH: _MODULE,
    }


@pytest.fixture
def small_vectors(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setattr(vs.settings, "QDRANT_VECTOR_DIM", _DIM)
    monkeypatch.setattr(vs.settings, "MILVUS_VECTOR_DIM", _DIM)
    monkeypatch.setattr(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.QDRANT)
    vs.close_vector_store_client()
    yield
    vs.close_vector_store_client()


@pytest.fixture
def ingestor() -> MagicMock:
    mock = MagicMock(spec=MemgraphIngestor)
    mock.fetch_all = MagicMock(return_value=[])
    return mock


@pytest.fixture
def repo(temp_repo: Path) -> Path:
    (temp_repo / _MODULE).write_text(_SOURCE_V1)
    return temp_repo


def _updater(
    repo: Path, ingestor: MagicMock, project: str = _PROJECT, **kwargs: bool
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
        **kwargs,
    )


def _sync(
    repo: Path,
    ingestor: MagicMock,
    graph: Mapping[int, str],
    project: str = _PROJECT,
    source: str | None = None,
    projects: tuple[str, ...] = (),
    embed: Callable[..., list[list[float]]] = _fake_embed_batch,
) -> None:
    """The embedding pass of one sync, with the graph holding `graph`.

    `graph` is what the project-prefixed embeddings query answers, which
    includes the functions of a registered project nested under this one;
    `projects` are the registered project names.
    """
    if source is not None:
        (repo / _MODULE).write_text(source)
    rows = [_row(n, qn) for n, qn in graph.items()]
    registered = [{cs.KEY_NAME: name} for name in projects]
    ingestor.fetch_all.return_value = rows
    ingestor.fetch_all.side_effect = (
        (
            lambda query, params=None: (
                registered if query == cq.CYPHER_LIST_PROJECTS else rows
            )
        )
        if projects
        else None
    )
    with (
        patch(
            "codebase_rag.graph_updater.has_semantic_dependencies", return_value=True
        ),
        patch("codebase_rag.embedder.embed_code_batch", side_effect=embed),
    ):
        _updater(repo, ingestor, project)._generate_semantic_embeddings()


@contextmanager
def _client() -> Iterator[vs.QdrantClient]:
    client = vs.get_qdrant_client()
    try:
        yield client
    finally:
        vs.close_vector_store_client()


def _points() -> list[tuple[str, int]]:
    """Every point in the collection, as (qualified name, node id)."""
    with _client() as client:
        points, _ = client.scroll(
            collection_name=vs.settings.QDRANT_COLLECTION_NAME,
            limit=1000,
            with_payload=True,
        )
    return sorted(
        (p.payload[cs.PAYLOAD_QUALIFIED_NAME], p.payload[cs.PAYLOAD_NODE_ID])
        for p in points
        if p.payload is not None
    )


def _point_ids() -> list[int | str]:
    with _client() as client:
        points, _ = client.scroll(
            collection_name=vs.settings.QDRANT_COLLECTION_NAME, limit=1000
        )
    return sorted((p.id for p in points), key=str)


def _write_legacy(points: Mapping[int, str]) -> None:
    """Points as the store wrote them before issue #2447: keyed by node id."""
    from qdrant_client.models import PointStruct

    with _client() as client:
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


@pytest.mark.usefixtures("small_vectors")
class TestASyncKeepsOnePointPerSymbol:
    def test_an_edit_replaces_the_symbols_point_instead_of_adding_one(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # The issue's reproduction: delete one function, sync, edit the
        # other, sync. Each re-parse re-creates keep_me under a new node id.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        assert _points() == [(_DELETE, 2), (_KEEP, 1)]

        _sync(repo, ingestor, {3: _KEEP}, source=_SOURCE_V2)
        assert _points() == [(_KEEP, 3)]

        _sync(repo, ingestor, {4: _KEEP}, source=_SOURCE_V3)
        assert _points() == [(_KEEP, 4)]

    def test_a_deleted_function_loses_its_point(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})

        # keep_me's file was not re-parsed here, so its node id is unchanged.
        _sync(repo, ingestor, {1: _KEEP})

        assert _points() == [(_KEEP, 1)]

    def test_the_last_function_removed_takes_the_last_point(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})

        _sync(repo, ingestor, {}, source="")

        assert _points() == []

    def test_search_answers_with_the_symbols_current_node(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # A stale point names a node id Memgraph may hand to an unrelated
        # node later; only the current one may come back.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        _sync(repo, ingestor, {3: _KEEP}, source=_SOURCE_V2)

        hits = vs.search_embeddings(_vector("keep_me"), top_k=10)

        assert [node_id for node_id, _ in hits] == [3]

    def test_an_unchanged_resync_keeps_the_same_points(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: a sync that changes nothing rewrites nothing either.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        before = _point_ids()

        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})

        assert _point_ids() == before
        assert _points() == [(_DELETE, 2), (_KEEP, 1)]

    def test_another_projects_points_survive_a_sync(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: a sync reconciles its own project's points only, even
        # when the other project's functions carry the same names.
        other_keep = f"{_OTHER}.m.keep_me"
        _sync(repo, ingestor, {7: other_keep}, project=_OTHER)

        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        _sync(repo, ingestor, {3: _KEEP}, source=_SOURCE_V2)

        assert _points() == [(other_keep, 7), (_KEEP, 3)]

    def test_a_failed_embedding_pass_keeps_the_unchanged_symbols(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: a model that fails for one sync must not empty the
        # store. The symbols are still in the graph at the same nodes, so
        # their points are still right.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})

        ingestor.fetch_all.return_value = [_row(1, _KEEP), _row(2, _DELETE)]
        with (
            patch(
                "codebase_rag.graph_updater.has_semantic_dependencies",
                return_value=True,
            ),
            patch(
                "codebase_rag.embedder.embed_code_batch",
                side_effect=RuntimeError("model unavailable"),
            ),
        ):
            _updater(repo, ingestor)._generate_semantic_embeddings()

        assert _points() == [(_DELETE, 2), (_KEEP, 1)]

    def test_a_sync_without_embeddings_leaves_the_store_alone(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: --no-embeddings writes nothing and removes nothing.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        ingestor.fetch_all.return_value = [_row(3, _KEEP)]

        _updater(repo, ingestor, skip_embeddings=True)._generate_semantic_embeddings()

        assert _points() == [(_DELETE, 2), (_KEEP, 1)]


@pytest.mark.usefixtures("small_vectors")
class TestDeleteProject:
    def test_delete_project_removes_every_point_of_the_project(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # `cgr delete-project` passes the ids of the nodes the graph still
        # holds; the points must go whatever those ids are.
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})

        vs.delete_project_embeddings(_PROJECT, [])

        assert _points() == []

    def test_delete_project_leaves_other_projects_alone(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: including a project whose name starts with this one's.
        _sync(repo, ingestor, {7: f"{_OTHER}.m.keep_me"}, project=_OTHER)
        _sync(repo, ingestor, {8: f"{_PROJECT}2.m.keep_me"}, project=f"{_PROJECT}2")
        _sync(repo, ingestor, {1: _KEEP})

        vs.delete_project_embeddings(_PROJECT, [1])

        assert _points() == [(f"{_OTHER}.m.keep_me", 7), (f"{_PROJECT}2.m.keep_me", 8)]


@pytest.mark.usefixtures("small_vectors")
class TestUpgradeFromNodeIdKeys:
    """A collection written before the fix holds points keyed by node id."""

    def test_a_sync_replaces_the_projects_node_id_keyed_points(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # What a few days of syncs left behind: two copies of keep_me and
        # the long-deleted delete_me.
        _write_legacy({1: _KEEP, 2: _DELETE, 5: _KEEP})

        _sync(repo, ingestor, {9: _KEEP}, source=_SOURCE_V2)

        assert _points() == [(_KEEP, 9)]
        assert all(isinstance(point_id, str) for point_id in _point_ids())

    def test_legacy_points_of_other_projects_are_left_for_their_own_sync(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: another project's old points, including one whose name
        # only starts with this project's, stay until that project syncs.
        _write_legacy({20: f"{_OTHER}.m.keep_me", 21: f"{_PROJECT}_x.m.keep_me"})

        _sync(repo, ingestor, {1: _KEEP})

        assert _points() == [
            (f"{_OTHER}.m.keep_me", 20),
            (_KEEP, 1),
            (f"{_PROJECT}_x.m.keep_me", 21),
        ]

    def test_legacy_points_still_answer_searches(self) -> None:
        # Negative: a collection nobody has re-synced yet keeps working.
        _write_legacy({20: f"{_OTHER}.m.keep_me"})

        hits = vs.search_embeddings(_vector(f"{_OTHER}.m.keep_me"), top_k=5)

        assert [node_id for node_id, _ in hits] == [20]

    @pytest.mark.parametrize("failure", ["embedding", "upsert"])
    def test_a_failed_first_sync_keeps_the_legacy_points_it_did_not_replace(
        self, repo: Path, ingestor: MagicMock, failure: str
    ) -> None:
        # Negative: the move to the new keys deletes a node-id keyed point
        # only once its symbol's new point is stored. A point whose symbol
        # is gone goes either way.
        _write_legacy({1: _KEEP, 2: _DELETE})

        def unavailable(
            snippets: list[str], **_: bool | int | str
        ) -> list[list[float]]:
            raise RuntimeError("model unavailable")

        if failure == "embedding":
            _sync(repo, ingestor, {1: _KEEP}, source=_SOURCE_V2, embed=unavailable)
        else:
            with patch.object(
                vs, "_upsert_with_retry", side_effect=RuntimeError("store down")
            ):
                _sync(repo, ingestor, {1: _KEEP}, source=_SOURCE_V2)

        assert _points() == [(_KEEP, 1)]
        assert _point_ids() == [1]

    def test_a_nested_projects_legacy_points_are_left_for_its_own_sync(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: `svc.` also prefixes `svc.v2`, a project of its own, so
        # svc's sync neither embeds svc.v2's functions nor deletes its old
        # points; the longest registered name owns a symbol (issue #1970).
        nested = f"{_PROJECT}.v2.m.keep_me"
        _write_legacy({1: _KEEP, 30: nested})

        _sync(
            repo,
            ingestor,
            {1: _KEEP, 30: nested},
            projects=(_PROJECT, f"{_PROJECT}.v2"),
        )

        assert _points() == [(_KEEP, 1), (nested, 30)]
        assert 30 in _point_ids()

    def test_delete_project_leaves_a_nested_projects_legacy_points(self) -> None:
        nested = f"{_PROJECT}.v2.m.keep_me"
        _write_legacy({1: _KEEP, 2: _DELETE, 30: nested})

        vs.delete_project_embeddings(_PROJECT, [1, 30], [f"{_PROJECT}.v2"])

        assert _points() == [(nested, 30)]

    def test_delete_project_removes_the_projects_legacy_points_too(self) -> None:
        # Including those of nodes the graph no longer holds, which the node
        # ids delete-project reads from the graph cannot name.
        _write_legacy({1: _KEEP, 2: _DELETE, 20: f"{_OTHER}.m.keep_me"})

        vs.delete_project_embeddings(_PROJECT, [1])

        assert _points() == [(f"{_OTHER}.m.keep_me", 20)]


def _has_milvus_lite() -> bool:
    import importlib.util

    return importlib.util.find_spec("milvus_lite") is not None


@pytest.mark.skipif(not has_pymilvus(), reason="pymilvus not installed")
@pytest.mark.skipif(not _has_milvus_lite(), reason="milvus-lite not installed")
class TestMilvus:
    """Milvus keeps its node-id primary key; a sync prunes what went stale."""

    @pytest.fixture(autouse=True)
    def milvus(
        self, small_vectors: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Generator[None, None, None]:
        monkeypatch.setattr(
            vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.MILVUS
        )
        monkeypatch.setattr(vs.settings, "MILVUS_URI", str(tmp_path / "milvus.db"))
        monkeypatch.setattr(vs.settings, "MILVUS_CONSISTENCY_LEVEL", "Strong")
        yield
        vs.close_vector_store_client()

    @staticmethod
    def _rows() -> list[tuple[str, int]]:
        client = vs.get_milvus_client()
        rows = client.query(
            collection_name=vs.settings.MILVUS_COLLECTION_NAME,
            filter=f"{cs.PAYLOAD_NODE_ID} >= 0",
            output_fields=[cs.PAYLOAD_NODE_ID, cs.PAYLOAD_QUALIFIED_NAME],
        )
        return sorted(
            (row[cs.PAYLOAD_QUALIFIED_NAME], row[cs.PAYLOAD_NODE_ID]) for row in rows
        )

    def test_an_edit_replaces_the_row_and_a_deleted_function_loses_it(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        _sync(repo, ingestor, {1: _KEEP, 2: _DELETE})
        _sync(repo, ingestor, {3: _KEEP}, source=_SOURCE_V2)

        assert self._rows() == [(_KEEP, 3)]

    def test_a_sync_leaves_a_nested_projects_rows_alone(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: `svc.v2` is a project of its own; svc's sync must not
        # read its rows as svc's stale ones.
        nested = f"{_PROJECT}.v2.m.keep_me"
        projects = (_PROJECT, f"{_PROJECT}.v2")
        _sync(repo, ingestor, {30: nested}, project=f"{_PROJECT}.v2", projects=projects)

        _sync(repo, ingestor, {1: _KEEP, 30: nested}, projects=projects)
        _sync(repo, ingestor, {3: _KEEP, 30: nested}, projects=projects)

        assert self._rows() == [(_KEEP, 3), (nested, 30)]

    def test_a_name_that_only_matches_as_a_pattern_is_not_this_project(
        self, repo: Path, ingestor: MagicMock
    ) -> None:
        # Negative: `_` is a wildcard in a Milvus LIKE; svc_a's sync must not
        # read svcXa's rows as its own.
        _sync(repo, ingestor, {7: "svcXa.m.keep_me"}, project="svcXa")
        _sync(repo, ingestor, {1: "svc_a.m.keep_me"}, project="svc_a")

        _sync(repo, ingestor, {}, project="svc_a", source="")

        assert self._rows() == [("svcXa.m.keep_me", 7)]
