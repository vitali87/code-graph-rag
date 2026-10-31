"""The `sqlite` vector store: semantic search with no server (sqlite-vec).

These run against a real sqlite-vec file. The store must answer as the Qdrant
and Milvus stores do: the same ordering and score semantics, the same project
scoping, the same failure handling, and the same refusal of a store whose
vector size differs from the configured one.
"""

from __future__ import annotations

import math
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Generator, Iterator, Sequence
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import vector_store as vs
from codebase_rag.config import AppConfig
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.sqlite_vectors import (
    SqliteVectorPoint,
    SqliteVectorRow,
    extensions_loadable,
    open_sqlite_vector_client,
)
from codebase_rag.types_defs import EmbeddingPoint, EmbeddingSymbol
from codebase_rag.utils.dependencies import has_sqlite_vec

pytestmark = [
    pytest.mark.skipif(not has_sqlite_vec(), reason="sqlite-vec not installed"),
    pytest.mark.skipif(
        not extensions_loadable(),
        reason="this Python's sqlite3 cannot load extensions",
    ),
]

_DIM = 4
_PROJECT = "pkg"


def _fn(qualified_name: str) -> EmbeddingSymbol:
    return EmbeddingSymbol(cs.NodeLabel.FUNCTION, qualified_name)


def _method(qualified_name: str) -> EmbeddingSymbol:
    return EmbeddingSymbol(cs.NodeLabel.METHOD, qualified_name)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    return dot / (math.hypot(*a) * math.hypot(*b))


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "store" / "vectors.sqlite"


@pytest.fixture(autouse=True)
def sqlite_backend(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    monkeypatch.setattr(vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.SQLITE)
    monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_PATH", store_path)
    monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_DIM", _DIM)
    vs.close_vector_store_client()
    yield
    vs.close_vector_store_client()


def _store(project: str, points: Sequence[EmbeddingPoint]) -> int:
    return vs.store_embedding_batch(project, points)


def _rows(path: Path) -> list[tuple[str, int, str]]:
    """Every stored point as (project, node id, qualified name).

    Read with a plain connection: the payload table needs no extension.
    """
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT project, node_id, qualified_name FROM cgr_vector_points"
        ).fetchall()
    return sorted(rows)


class TestStoreAndSearch:
    def test_search_ranks_by_cosine_similarity_nearest_first(self) -> None:
        stored = _store(
            _PROJECT,
            [
                (101, [1.0, 0.0, 0.0, 0.0], _fn("pkg.auth.login")),
                (102, [0.0, 1.0, 0.0, 0.0], _fn("pkg.billing.charge")),
                (103, [0.9, 0.1, 0.0, 0.0], _fn("pkg.auth.refresh")),
            ],
        )

        results = vs.search_embeddings([0.95, 0.05, 0.0, 0.0], top_k=3)

        assert stored == 3
        assert [node_id for node_id, _ in results] == [101, 103, 102]
        scores = [score for _, score in results]
        assert scores == sorted(scores, reverse=True)

    def test_the_score_is_the_cosine_similarity_qdrant_reports(self) -> None:
        # Callers rank and print the score; Qdrant and Milvus answer cosine
        # similarity, higher for nearer, not vec0's distance.
        vectors = {
            1: [1.0, 0.0, 0.0, 0.0],
            2: [0.0, 1.0, 0.0, 0.0],
            3: [-1.0, 0.0, 0.0, 0.0],
            4: [0.6, 0.8, 0.0, 0.0],
        }
        _store(_PROJECT, [(n, v, _fn(f"pkg.m.f{n}")) for n, v in vectors.items()])
        query = [2.0, 0.0, 0.0, 0.0]

        scores = dict(vs.search_embeddings(query, top_k=4))

        for node_id, vector in vectors.items():
            assert scores[node_id] == pytest.approx(_cosine(query, vector), abs=1e-6)

    def test_top_k_defaults_to_the_configured_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_TOP_K", 2)
        _store(
            _PROJECT,
            [(n, [1.0, float(n), 0.0, 0.0], _fn(f"pkg.m.f{n}")) for n in range(5)],
        )

        assert len(vs.search_embeddings([1.0, 0.0, 0.0, 0.0])) == 2

    def test_an_empty_store_answers_no_hits(self) -> None:
        assert vs.search_embeddings([0.5] * _DIM, top_k=5) == []

    def test_a_top_k_past_the_vec0_limit_is_clamped_not_refused(self) -> None:
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.f"))])

        results = vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=10_000)

        assert [node_id for node_id, _ in results] == [1]

    def test_a_zero_vector_is_stored_but_takes_no_neighbours_place(self) -> None:
        # vec0 answers a NULL distance for a vector with no direction, and
        # ranks it ahead of every real neighbour.
        symbols = {1: _fn("pkg.m.empty"), 2: _fn("pkg.m.f")}
        _store(
            _PROJECT,
            [
                (1, [0.0, 0.0, 0.0, 0.0], symbols[1]),
                (2, [1.0, 0.0, 0.0, 0.0], symbols[2]),
            ],
        )

        results = vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=1)

        assert [node_id for node_id, _ in results] == [2]
        assert vs.verify_stored_ids(_PROJECT, symbols) == {1, 2}

    def test_a_zero_query_matches_nothing(self) -> None:
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.f"))])

        assert vs.search_embeddings([0.0] * _DIM, top_k=5) == []

    def test_a_symbol_whose_vector_turns_zero_stops_matching(self) -> None:
        # Negative: its old vector must not keep answering for it.
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.f"))])
        _store(_PROJECT, [(2, [0.0, 0.0, 0.0, 0.0], _fn("pkg.m.f"))])

        assert vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=5) == []

    def test_storing_a_symbol_again_replaces_its_point(self, store_path: Path) -> None:
        # The same function, re-parsed under a new node id with a new body.
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.func"))])
        _store(_PROJECT, [(2, [0.0, 1.0, 0.0, 0.0], _fn("pkg.func"))])

        results = vs.search_embeddings([0.0, 1.0, 0.0, 0.0], top_k=5)

        assert len(results) == 1
        assert results[0][0] == 2
        assert results[0][1] > 0.99
        assert _rows(store_path) == [(_PROJECT, 2, "pkg.func")]

    def test_a_function_and_a_method_sharing_a_name_keep_a_point_each(
        self, store_path: Path
    ) -> None:
        _store(
            _PROJECT,
            [
                (1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.Clock.now")),
                (2, [0.9, 0.1, 0.0, 0.0], _method("pkg.m.Clock.now")),
            ],
        )

        hits = vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=5)

        assert {node_id for node_id, _ in hits} == {1, 2}
        assert len(_rows(store_path)) == 2

    def test_the_vectors_survive_reopening_the_file(self, store_path: Path) -> None:
        _store(
            _PROJECT,
            [
                (1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.a")),
                (2, [0.0, 1.0, 0.0, 0.0], _fn("pkg.m.b")),
            ],
        )
        vs.close_vector_store_client()

        results = vs.search_embeddings([0.0, 1.0, 0.0, 0.0], top_k=1)
        found = vs.verify_stored_ids(_PROJECT, {1: _fn("pkg.m.a"), 2: _fn("pkg.m.b")})

        assert store_path.is_file()
        assert [node_id for node_id, _ in results] == [2]
        assert found == {1, 2}

    def test_two_processes_share_the_file(self, store_path: Path) -> None:
        # Embedded Qdrant locks its folder to one process, so an MCP server
        # and an indexing run could not both use it. A second process holds a
        # connection of its own, as this second client does.
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.a"))])
        other = open_sqlite_vector_client(store_path)
        try:
            other.upsert(
                _PROJECT,
                [
                    SqliteVectorRow(
                        vs.embedding_point_id(_PROJECT, _fn("pkg.m.b")),
                        2,
                        "pkg.m.b",
                        [0.0, 1.0, 0.0, 0.0],
                    )
                ],
            )
            seen_by_other = [hit.node_id for hit in other.search([1.0, 0, 0, 0], 5)]
        finally:
            other.close()

        hits = vs.search_embeddings([0.0, 1.0, 0.0, 0.0], top_k=5)

        assert seen_by_other == [1, 2]
        assert [node_id for node_id, _ in hits] == [2, 1]

    def test_concurrent_searches_share_the_one_connection(self) -> None:
        # The MCP server searches from worker threads against one cached
        # client.
        _store(
            _PROJECT,
            [(n, [1.0, float(n), 0.0, 0.0], _fn(f"pkg.m.f{n}")) for n in range(20)],
        )
        answers: list[list[tuple[int, float]]] = []
        errors: list[BaseException] = []

        def search() -> None:
            try:
                for _ in range(20):
                    answers.append(vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=3))
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=search) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(answers) == 160
        assert all([node_id for node_id, _ in a] == [0, 1, 2] for a in answers)


class TestProjectScope:
    def test_a_project_search_keeps_only_that_projects_hits(self) -> None:
        _store(
            "user-service",
            [
                (1, [1.0, 0.0, 0.0, 0.0], _fn("user-service.handlers.get_user")),
                (3, [0.8, 0.2, 0.0, 0.0], _fn("user-service.models.User")),
            ],
        )
        _store(
            "order-service",
            [(2, [0.95, 0.05, 0.0, 0.0], _fn("order-service.orders.create"))],
        )
        # A name that only starts with the project's is another project.
        _store(
            "user-service2",
            [(4, [0.99, 0.01, 0.0, 0.0], _fn("user-service2.handlers.get"))],
        )

        results = vs.search_embeddings(
            [1.0, 0.0, 0.0, 0.0], top_k=2, project="user-service"
        )

        assert [node_id for node_id, _ in results] == [1, 3]

    def test_a_search_without_a_project_spans_every_project(self) -> None:
        _store("a", [(1, [1.0, 0.0, 0.0, 0.0], _fn("a.m.f"))])
        _store("b", [(2, [0.9, 0.1, 0.0, 0.0], _fn("b.m.f"))])

        results = vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=5)

        assert [node_id for node_id, _ in results] == [1, 2]

    def test_the_window_widens_until_the_projects_matches_are_found(self) -> None:
        # The first over-fetch window holds only other projects' neighbours.
        noise = [
            (n, [1.0, 0.001 * n, 0.0, 0.0], _fn(f"noise.mod.f{n}")) for n in range(100)
        ]
        _store("noise", noise)
        _store(
            "wanted",
            [
                (1000, [0.5, 0.0, 1.0, 0.0], _fn("wanted.m.a")),
                (1001, [0.0, 0.0, 1.0, 0.0], _fn("wanted.m.b")),
            ],
        )

        results = vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=2, project="wanted")

        assert [node_id for node_id, _ in results] == [1000, 1001]


class TestVerifyStoredIds:
    def test_finds_the_nodes_whose_points_are_stored(self) -> None:
        symbols = {101: _fn("pkg.a"), 102: _fn("pkg.b"), 999: _fn("pkg.gone")}
        _store(
            _PROJECT,
            [
                (101, [1.0, 0.0, 0.0, 0.0], symbols[101]),
                (102, [0.0, 1.0, 0.0, 0.0], symbols[102]),
            ],
        )

        assert vs.verify_stored_ids(_PROJECT, symbols) == {101, 102}

    def test_a_point_that_names_another_node_is_not_found(self) -> None:
        # The symbol's point still names the node it had before a re-parse.
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.a"))])

        assert vs.verify_stored_ids(_PROJECT, {5: _fn("pkg.a")}) == set()

    def test_another_projects_point_of_the_same_name_is_not_found(self) -> None:
        _store("other", [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.a"))])

        assert vs.verify_stored_ids(_PROJECT, {1: _fn("pkg.a")}) == set()

    def test_returns_empty_for_empty_input(self) -> None:
        assert vs.verify_stored_ids(_PROJECT, {}) == set()


class TestDeleteProjectEmbeddings:
    def test_removes_every_point_of_the_project_and_nothing_else(
        self, store_path: Path
    ) -> None:
        _store(
            _PROJECT,
            [
                (1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.a")),
                (2, [0.0, 1.0, 0.0, 0.0], _fn("pkg.m.b")),
            ],
        )
        _store("other", [(7, [1.0, 0.0, 0.0, 0.0], _fn("other.m.a"))])
        _store("pkg2", [(8, [1.0, 0.0, 0.0, 0.0], _fn("pkg2.m.a"))])
        # A nested project keeps its points: they are stored for it.
        _store("pkg.v2", [(30, [1.0, 0.0, 0.0, 0.0], _fn("pkg.v2.m.a"))])

        # The node ids the graph still holds: whatever they are, the
        # project's points go.
        deleted = vs.delete_project_embeddings(_PROJECT, [1, 30], ["pkg.v2"])

        assert deleted is True
        assert _rows(store_path) == [
            ("other", 7, "other.m.a"),
            ("pkg.v2", 30, "pkg.v2.m.a"),
            ("pkg2", 8, "pkg2.m.a"),
        ]
        assert {n for n, _ in vs.search_embeddings([1.0, 0.0, 0.0, 0.0], 10)} == {
            7,
            8,
            30,
        }

    def test_reports_nothing_to_delete_as_done(self) -> None:
        assert vs.delete_project_embeddings(_PROJECT, []) is True

    def test_reports_a_failed_lookup(self) -> None:
        client = MagicMock()
        client.project_points.side_effect = sqlite3.OperationalError("locked")

        with (
            patch.object(vs, "get_sqlite_vector_client", return_value=client),
            patch.object(vs, "logger") as logger,
        ):
            deleted = vs.delete_project_embeddings(_PROJECT, [1, 2])

        assert deleted is False
        client.delete.assert_not_called()
        logger.warning.assert_called_once()

    def test_reports_a_failed_delete(self) -> None:
        # Retiring a project must know its vectors are still there, or it
        # deletes the node ids they are keyed by (review of PR 2497).
        client = MagicMock()
        client.project_points.return_value = [SqliteVectorPoint("p-1", 1)]
        client.delete.side_effect = sqlite3.OperationalError("locked")

        with patch.object(vs, "get_sqlite_vector_client", return_value=client):
            deleted = vs.delete_project_embeddings(_PROJECT, [1])

        assert deleted is False


class TestDeleteStaleEmbeddings:
    def test_deletes_what_no_longer_matches_the_graph(self, store_path: Path) -> None:
        _store(
            _PROJECT,
            [
                (1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.kept")),
                # Re-created under node 2 since; the point names the old node.
                (9, [0.0, 1.0, 0.0, 0.0], _fn("pkg.m.moved")),
                (3, [0.0, 0.0, 1.0, 0.0], _fn("pkg.m.gone")),
            ],
        )
        _store("other", [(7, [1.0, 0.0, 0.0, 0.0], _fn("other.m.gone"))])

        removed = vs.delete_stale_embeddings(
            _PROJECT, {1: _fn("pkg.m.kept"), 2: _fn("pkg.m.moved")}
        )

        assert removed == 2
        assert _rows(store_path) == [
            ("other", 7, "other.m.gone"),
            (_PROJECT, 1, "pkg.m.kept"),
        ]

    def test_deletes_nothing_when_everything_matches(self, store_path: Path) -> None:
        _store(_PROJECT, [(1, [1.0, 0.0, 0.0, 0.0], _fn("pkg.m.kept"))])

        assert vs.delete_stale_embeddings(_PROJECT, {1: _fn("pkg.m.kept")}) == 0
        assert _rows(store_path) == [(_PROJECT, 1, "pkg.m.kept")]

    def test_drops_the_method_the_graph_lost_beside_its_namesake(
        self, store_path: Path
    ) -> None:
        function, method = _fn("pkg.m.C.f"), _method("pkg.m.C.f")
        _store(
            _PROJECT,
            [(1, [1.0, 0.0, 0.0, 0.0], function), (2, [0.0, 1.0, 0.0, 0.0], method)],
        )

        removed = vs.delete_stale_embeddings(_PROJECT, {1: function})

        assert removed == 1
        assert _rows(store_path) == [(_PROJECT, 1, "pkg.m.C.f")]

    def test_a_nested_projects_points_are_left_for_its_own_sync(
        self, store_path: Path
    ) -> None:
        _store("pkg.v2", [(30, [1.0, 0.0, 0.0, 0.0], _fn("pkg.v2.m.a"))])

        removed = vs.delete_stale_embeddings(_PROJECT, {}, nested_projects=["pkg.v2"])

        assert removed == 0
        assert _rows(store_path) == [("pkg.v2", 30, "pkg.v2.m.a")]


class TestFailures:
    def test_a_failed_search_answers_no_hits(self) -> None:
        client = MagicMock()
        client.search.side_effect = sqlite3.OperationalError("disk I/O error")

        with (
            patch.object(vs, "get_sqlite_vector_client", return_value=client),
            patch.object(vs, "logger") as logger,
        ):
            assert vs.search_embeddings([0.2] * _DIM) == []

        logger.warning.assert_called_once()

    def test_a_failed_batch_is_counted_as_nothing_stored(self) -> None:
        client = MagicMock()
        client.upsert.side_effect = sqlite3.OperationalError("database is locked")

        with patch.object(vs, "get_sqlite_vector_client", return_value=client):
            stored = _store(_PROJECT, [(1, [0.1] * _DIM, _fn("pkg.f"))])

        assert stored == 0

    def test_a_batch_that_fails_partway_writes_none_of_its_points(
        self, store_path: Path
    ) -> None:
        # One transaction per batch: a point whose vector vec0 refuses must
        # not leave the points before it half-stored.
        rows = [
            SqliteVectorRow("p-1", 1, "pkg.a", [1.0, 0.0, 0.0, 0.0]),
            SqliteVectorRow("p-2", 2, "pkg.b", [1.0, 0.0, 0.0]),
        ]

        client = vs.get_sqlite_vector_client()

        with pytest.raises(sqlite3.OperationalError, match="Dimension mismatch"):
            client.upsert(_PROJECT, rows)

        assert _rows(store_path) == []

    def test_a_missing_sqlite_vec_leaves_semantic_search_off(self) -> None:
        with (
            patch.object(vs, "has_sqlite_vec", return_value=False),
            patch.object(vs, "logger") as logger,
        ):
            assert vs.search_embeddings([0.2] * _DIM) == []
            assert vs.store_embedding_batch(_PROJECT, []) == 0

        logger.warning.assert_called()

    def test_opening_the_store_without_sqlite_vec_says_how_to_install_it(
        self,
    ) -> None:
        with patch.object(vs, "has_sqlite_vec", return_value=False):
            with pytest.raises(RuntimeError, match=r"code-graph-rag\[sqlite-vec\]"):
                vs.get_sqlite_vector_client()

    def test_a_python_that_cannot_load_extensions_is_named(self) -> None:
        from codebase_rag import sqlite_vectors

        with patch.object(sqlite_vectors, "extensions_loadable", return_value=False):
            with pytest.raises(RuntimeError, match="extension loading"):
                vs.get_sqlite_vector_client()

        assert vs._CLIENT is None

    def test_a_python_that_cannot_load_extensions_has_no_sqlite_store(
        self,
    ) -> None:
        # As without sqlite-vec: semantic search is off, so a sync does not
        # owe a pass it can never pay, and `--clean` does not fail after the
        # graph is already wiped. The reason is logged once.
        from codebase_rag import sqlite_vectors
        from codebase_rag.utils import dependencies

        dependencies._warn_sqlite_extensions_unsupported.cache_clear()
        messages: list[str] = []
        sink = logger.add(
            lambda message: messages.append(message.record["message"]),
            level="WARNING",
        )
        try:
            with patch.object(
                sqlite_vectors, "extensions_loadable", return_value=False
            ):
                assert dependencies.has_vector_store_dependencies() is False
                assert vs._get_vector_store() is None
                vs.clear_all_embeddings()
        finally:
            logger.remove(sink)
            dependencies._warn_sqlite_extensions_unsupported.cache_clear()

        assert [m for m in messages if "extension loading" in m] == [
            ex.SQLITE_VEC_EXTENSIONS_UNSUPPORTED
        ]

    def test_switching_to_sqlite_closes_the_other_backends_client(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other = MagicMock()
        monkeypatch.setattr(vs, "_CLIENT", other)
        monkeypatch.setattr(vs, "_CLIENT_BACKEND", VectorStoreBackend.QDRANT)

        client = vs.get_sqlite_vector_client()

        other.close.assert_called_once()
        assert vs._CLIENT is client


class TestVectorSize:
    @staticmethod
    def _make_store_of_size(dim: int, monkeypatch: pytest.MonkeyPatch) -> None:
        with monkeypatch.context() as m:
            m.setattr(vs.settings, "SQLITE_VECTOR_DIM", dim)
            vs.get_sqlite_vector_client()
            vs.close_vector_store_client()

    def test_a_store_of_another_size_is_refused_by_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._make_store_of_size(8, monkeypatch)

        with pytest.raises(ValueError, match="dimension 8, expected 4"):
            vs.get_sqlite_vector_client()
        # The refused client is not cached, so the check runs again rather
        # than being skipped.
        assert vs._CLIENT is None
        with pytest.raises(ValueError, match="SQLITE_VECTOR_DIM"):
            vs.get_sqlite_vector_client()

    def test_a_store_of_the_same_size_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._make_store_of_size(_DIM, monkeypatch)

        assert vs.get_sqlite_vector_client().dimension() == _DIM

    def test_clean_rebuilds_a_store_of_the_wrong_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with monkeypatch.context() as m:
            m.setattr(vs.settings, "SQLITE_VECTOR_DIM", 8)
            _store(_PROJECT, [(1, [1.0] * 8, _fn("pkg.a"))])
            vs.close_vector_store_client()

        vs.clear_all_embeddings()
        stored = _store(_PROJECT, [(2, [1.0, 0.0, 0.0, 0.0], _fn("pkg.b"))])
        found = vs.verify_stored_ids(_PROJECT, {1: _fn("pkg.a"), 2: _fn("pkg.b")})

        assert stored == 1
        assert found == {2}
        assert vs.get_sqlite_vector_client().dimension() == _DIM

    def test_clear_all_embeddings_empties_every_project(self, store_path: Path) -> None:
        _store("a", [(1, [1.0, 0.0, 0.0, 0.0], _fn("a.m.f"))])
        _store("b", [(2, [1.0, 0.0, 0.0, 0.0], _fn("b.m.f"))])

        vs.clear_all_embeddings()

        assert _rows(store_path) == []
        assert vs.search_embeddings([1.0, 0.0, 0.0, 0.0], top_k=5) == []

    def test_a_failed_clean_does_not_leave_its_unvalidated_client_cached(
        self,
    ) -> None:
        client = MagicMock()
        client.dimension.return_value = 8
        client.recreate_collection.side_effect = sqlite3.OperationalError("locked")

        with patch.object(vs, "open_sqlite_vector_client", return_value=client):
            with pytest.raises(sqlite3.OperationalError):
                vs.clear_all_embeddings()

        assert vs._CLIENT is None
        client.close.assert_called_once()

    def test_vectors_of_the_wrong_size_name_the_setting_to_change(self) -> None:
        points = [(1, [0.1, 0.2, 0.3], _fn("pkg.a"))]

        with pytest.raises(ValueError, match="3-dimensional.*SQLITE_VECTOR_DIM"):
            _store(_PROJECT, points)
        with pytest.raises(ValueError, match="SQLITE_VECTOR_DIM"):
            vs.search_embeddings([0.1, 0.2])


class TestWhereTheFileGoes:
    def test_unset_it_sits_under_cgr_home_beside_the_graph(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_PATH", None)
        monkeypatch.setattr(vs.settings, "SQLITE_GRAPH_PATH", None)
        monkeypatch.setattr(vs.settings, "CGR_HOME", tmp_path / "home")

        path = vs.settings.vector_db_file

        assert path == tmp_path / "home" / cs.SQLITE_VEC_DEFAULT_DB_FILENAME
        assert path.parent == vs.settings.graph_db_file.parent

    def test_a_set_path_is_used_with_the_home_directory_expanded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_PATH", Path("~/v.sqlite"))

        assert vs.settings.vector_db_file == Path.home() / "v.sqlite"

    def test_opening_the_store_creates_its_folder(self, store_path: Path) -> None:
        assert not store_path.parent.exists()

        vs.get_sqlite_vector_client()

        assert store_path.is_file()

    def test_the_embedding_cache_sits_beside_the_store(self, store_path: Path) -> None:
        # Not in the cwd-relative Qdrant folder, which would put a hidden
        # folder into the indexed repository (issue #2355).
        assert vs.embedding_cache_dir() == store_path.parent


def test_importing_the_vector_store_does_not_load_sqlite_vec() -> None:
    # sqlite-vec is optional and loaded only when a sqlite store opens.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, codebase_rag.vector_store; print('sqlite_vec' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        encoding=cs.ENCODING_UTF8,
        timeout=120,
        check=True,
    )

    assert result.stdout.strip().splitlines()[-1] == "False"


class TestTheDefaultStore:
    """The `sqlite` store is the default, as the embedded graph is: a fresh
    install stores and searches vectors with no server and no Docker. An
    install upgraded in place from when Qdrant was the default keeps its
    QDRANT_* settings and never set CGR_VECTOR_STORE_BACKEND. Its embeddings
    are still in Qdrant while cgr now opens an empty file, so it is told so,
    once."""

    @pytest.fixture
    def notes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Iterator[list[str]]:
        for name in (
            "CGR_VECTOR_STORE_BACKEND",
            cs.SETTING_VECTOR_STORE_BACKEND,
            *cs.QDRANT_CONNECTION_SETTINGS,
        ):
            monkeypatch.delenv(name, raising=False)
        # AppConfig reads `.env` from the working directory.
        monkeypatch.chdir(tmp_path)
        vs._note_qdrant_set_without_backend.cache_clear()
        messages: list[str] = []
        sink = logger.add(
            lambda message: messages.append(message.record["message"]),
            level="WARNING",
        )
        yield messages
        logger.remove(sink)
        vs._note_qdrant_set_without_backend.cache_clear()

    @staticmethod
    def _open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(vs, "settings", AppConfig(CGR_HOME=tmp_path))
        vs.close_vector_store_client()
        vs.get_sqlite_vector_client()

    def test_sqlite_is_the_default(self, notes: list[str], tmp_path: Path) -> None:
        config = AppConfig(CGR_HOME=tmp_path)
        assert config.VECTOR_STORE_BACKEND == VectorStoreBackend.SQLITE
        assert config.vector_db_file == tmp_path / "vectors.sqlite"

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("QDRANT_URL", "http://qdrant.internal:6333"),
            ("QDRANT_DB_PATH", "./.qdrant_code_embeddings"),
        ],
    )
    def test_qdrant_settings_without_a_backend_are_noted_once(
        self,
        notes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        name: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(name, value)
        self._open(monkeypatch, tmp_path)
        self._open(monkeypatch, tmp_path)
        assert len(notes) == 1
        assert str(tmp_path / "vectors.sqlite") in notes[0]
        assert "CGR_VECTOR_STORE_BACKEND=qdrant" in notes[0]

    @pytest.mark.parametrize(
        "env",
        [
            # Nothing chosen: a first install, nothing to warn about.
            {},
            # An explicit choice settles it.
            {
                "QDRANT_URL": "http://qdrant.internal:6333",
                "CGR_VECTOR_STORE_BACKEND": "sqlite",
            },
        ],
    )
    def test_a_settled_choice_is_not_noted(
        self,
        notes: list[str],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        env: dict[str, str],
    ) -> None:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        self._open(monkeypatch, tmp_path)
        assert notes == []
