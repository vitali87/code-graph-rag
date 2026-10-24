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

import errno
import hashlib
import json
import sqlite3
from collections.abc import Callable, Generator, Iterator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import graph_updater as gu
from codebase_rag import vector_store as vs
from codebase_rag.checkout_state import state_file
from codebase_rag.constants import VectorStoreBackend
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.sqlite_vectors import extensions_loadable
from codebase_rag.types_defs import PropertyParams, ResultRow
from codebase_rag.utils.dependencies import (
    has_pymilvus,
    has_qdrant_client,
    has_sqlite_vec,
)
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


def _updater(
    repo: Path,
    graph: _Graph,
    skip_embeddings: bool = False,
    project: str = _PROJECT,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=graph,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=project,
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
    project: str = _PROJECT,
) -> GraphUpdater:
    """One sync of `repo` (a file for a single-file run), the graph's
    functions by node id as `functions` says."""
    if source is not None:
        (repo / _MODULE).write_text(source)
    graph.rows = (
        [_row(node_id, qn) for node_id, qn in functions.items()]
        if isinstance(functions, Mapping)
        else list(functions)
    )
    updater = _updater(repo, graph, skip_embeddings, project)
    with (
        _semantic_extra(),
        patch("codebase_rag.embedder.embed_code_batch", side_effect=embed),
    ):
        updater.run()
    return updater


def _in_sync(
    repo: Path,
    graph: _Graph,
    skip_embeddings: bool = False,
    project: str = _PROJECT,
) -> bool:
    """What the next sync's fast-path check answers, before it runs."""
    with _semantic_extra():
        updater = _updater(repo, graph, skip_embeddings, project)
        return updater._is_already_in_sync()


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
        assert _pending(repo) == set()


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


def _store_stamps(repo: Path) -> list[Path]:
    """The per-project files naming the backend a project's points are in."""
    pattern = cs.EMBEDDINGS_STORE_PROJECT_TEMPLATE.format(digest="*")
    return sorted(state_file(repo, "").glob(pattern))


def _sqlite_points(path: Path) -> list[tuple[str, int]]:
    with closing(sqlite3.connect(path)) as conn:
        return sorted(
            conn.execute("SELECT qualified_name, node_id FROM cgr_vector_points")
        )


class TestASwitchedStoreIsRefilled:
    """The points stay in the store they were written to. When the selected
    backend changes, as when `sqlite` replaced Qdrant as the default, an
    unchanged checkout would keep the in-sync fast path, which never embeds,
    and semantic search would find nothing until some file changed."""

    def test_a_settled_pass_keeps_the_fast_path(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: the stamp names the backend the pass wrote to.
        _sync(repo, graph, {1: _KEEP})

        assert [p.read_text() for p in _store_stamps(repo)] == ["qdrant"]
        assert _in_sync(repo, graph) is True

    @pytest.mark.skipif(not has_sqlite_vec(), reason="sqlite-vec not installed")
    @pytest.mark.skipif(
        not extensions_loadable(), reason="this Python's sqlite3 cannot load extensions"
    )
    # Unstamped: synced before the stamp existed, when Qdrant was the default,
    # as an install that now opens the sqlite default was.
    @pytest.mark.parametrize("stamped", [True, False], ids=["stamped", "unstamped"])
    def test_an_unchanged_sync_fills_a_newly_selected_store(
        self,
        repo: Path,
        graph: _Graph,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stamped: bool,
    ) -> None:
        _sync(repo, graph, {1: _KEEP})
        if not stamped:
            for stamp in _store_stamps(repo):
                stamp.unlink()
        store = tmp_path / "vectors.sqlite"
        monkeypatch.setattr(
            vs.settings, "VECTOR_STORE_BACKEND", VectorStoreBackend.SQLITE
        )
        monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_PATH", store)
        monkeypatch.setattr(vs.settings, "SQLITE_VECTOR_DIM", _DIM)
        vs.close_vector_store_client()

        assert _in_sync(repo, graph) is False
        refill = _sync(repo, graph, {1: _KEEP})
        vs.close_vector_store_client()

        assert refill.skipped_because_in_sync is False
        assert _sqlite_points(store) == [(_KEEP, 1)]
        assert _in_sync(repo, graph) is True

    def test_a_checkout_synced_before_the_stamp_keeps_its_qdrant_store(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: its points went to Qdrant, the default then, and an
        # install that chose Qdrant still searches them there.
        _sync(repo, graph, {1: _KEEP})
        for stamp in _store_stamps(repo):
            stamp.unlink()

        assert _in_sync(repo, graph) is True

    def test_a_sync_without_embeddings_neither_owes_nor_stamps(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: a run that cannot embed keeps the fast path, as for the
        # pending marker, and leaves the pass to the next run that can.
        _sync(repo, graph, {1: _KEEP}, skip_embeddings=True)

        assert _store_stamps(repo) == []
        assert _in_sync(repo, graph, skip_embeddings=True) is True


_OTHER = "other"
_OTHER_KEEP = f"{_OTHER}.m.keep_me"


def _shared_marker(repo: Path) -> Path:
    """The marker an earlier build shared between every project."""
    return state_file(repo, cs.EMBEDDINGS_PENDING_FILENAME)


def _pending(repo: Path) -> set[str]:
    """The projects that owe a pass, by the per-project markers."""
    pattern = cs.EMBEDDINGS_PENDING_PROJECT_TEMPLATE.format(digest="*")
    return {
        marker.read_text(encoding="utf-8")
        for marker in state_file(repo, "").glob(pattern)
    }


def _owe(repo: Path, project: str) -> None:
    gu._save_embeddings_owed(
        gu._embeddings_marker(state_file(repo, ""), project), project, True
    )


def _interrupting_batch(number: int) -> Embed:
    """An embedder that a Ctrl+C stops at its `number`th batch."""
    calls = 0

    def embed(snippets: list[str], **_: bool | int | str) -> list[list[float]]:
        nonlocal calls
        calls += 1
        if calls == number:
            raise KeyboardInterrupt
        return _fake_embed_batch(snippets)

    return embed


class TestAnInterruptedPassIsRetried:
    """Ctrl+C in the pass commits the run, then stops (issue #2442); the
    functions it had not reached yet must not stay missing."""

    @pytest.fixture(autouse=True)
    def one_function_per_batch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(vs.settings, "QDRANT_BATCH_SIZE", 1)

    def _interrupted(self, repo: Path, graph: _Graph) -> None:
        embed = _interrupting_batch(2)
        with pytest.raises(ex.EmbeddingsInterrupted):
            _sync(repo, graph, {1: _KEEP, 2: _DELETE}, embed=embed)
        assert _points() == [(_KEEP, 1)]

    def test_an_unchanged_sync_embeds_what_an_interrupt_left_out(
        self, repo: Path, graph: _Graph
    ) -> None:
        self._interrupted(repo, graph)

        retry = _sync(repo, graph, {1: _KEEP, 2: _DELETE})

        assert retry.skipped_because_in_sync is False
        assert _points() == [(_DELETE, 2), (_KEEP, 1)]
        assert _in_sync(repo, graph) is True

    def test_an_uninterrupted_pass_clears_what_an_interrupt_owed(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: the debt is the interrupted project's, and its next
        # complete pass settles it.
        self._interrupted(repo, graph)
        assert _pending(repo) == {_PROJECT}

        _sync(repo, graph, {1: _KEEP, 2: _DELETE})

        assert _pending(repo) == set()

    def test_an_interrupted_run_without_embeddings_writes_nothing(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: a --skip-embeddings run never enters the pass, so a
        # Ctrl+C anywhere in it owes no embeddings.
        _sync(repo, graph, {1: _KEEP, 2: _DELETE}, skip_embeddings=True)
        with (
            patch.object(
                GraphUpdater, "_prune_orphan_nodes", side_effect=KeyboardInterrupt
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            _sync(
                repo,
                graph,
                {3: _KEEP},
                source=_SOURCE_KEEP_ONLY,
                skip_embeddings=True,
            )

        assert _pending(repo) == set()


class TestEachProjectOwesItsOwn:
    """One checkout indexed under two project names keeps two debts apart."""

    def test_another_projects_clean_pass_keeps_this_projects_retry(
        self, repo: Path, graph: _Graph
    ) -> None:
        # svc's pass fails; then other, indexed from the same tree, runs a
        # clean pass. A single-file run of it leaves svc's stamp and cache
        # as they were, so svc's next sync is unchanged and would take the
        # fast path if other's pass had settled svc's debt.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        with _store_down():
            _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
        _sync(repo / _MODULE, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert (_OTHER_KEEP, 7) in _points()

        retry = _sync(repo, graph, {2: _KEEP})

        assert retry.skipped_because_in_sync is False
        assert (_KEEP, 2) in _points()

    def test_a_clean_pass_racing_another_projects_failure_keeps_its_retry(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Two syncs of one checkout at once, sequenced: other's clean pass
        # has decided what to write when svc's failed pass records its debt,
        # and other's write lands after it. svc's next sync must retry.
        _sync(repo, graph, {1: _KEEP}, source=_SOURCE_KEEP_ONLY)
        real_save = gu._save_embeddings_owed
        armed = True

        def save_after_svc_fails(*args: Path | str | bool | frozenset[str]) -> None:
            nonlocal armed
            if armed:
                armed = False
                with _store_down():
                    _sync(repo, graph, {2: _KEEP}, source=_SOURCE_KEEP_EDITED)
            real_save(*args)

        with patch.object(gu, "_save_embeddings_owed", save_after_svc_fails):
            _sync(repo / _MODULE, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert armed is False

        retry = _sync(repo, graph, {2: _KEEP})

        assert retry.skipped_because_in_sync is False
        assert (_KEEP, 2) in _points()

    def test_a_projects_own_clean_pass_settles_its_debt(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: other's debt is other's to clear, and its clean retry
        # does, taking the marker with it once nobody else owes.
        _sync(repo, graph, {1: _KEEP})
        with _store_down():
            _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        _owe(repo, _PROJECT)
        assert _pending(repo) == {_OTHER, _PROJECT}

        retry = _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)

        assert retry.skipped_because_in_sync is False
        assert _pending(repo) == {_PROJECT}
        assert _in_sync(repo, graph, project=_OTHER) is True

    def test_this_projects_clean_pass_keeps_another_projects_debt(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: the mirror of the first test. svc's clean single-file
        # pass leaves other's stamp in place, so the marker alone is what
        # still sends other's unchanged sync through the passes.
        _sync(repo, graph, {1: _KEEP})
        with _store_down():
            _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)

        _sync(repo / _MODULE, graph, {1: _KEEP})

        assert _pending(repo) == {_OTHER}
        assert _in_sync(repo, graph, project=_OTHER) is False

    @pytest.mark.parametrize("shared", [False, True])
    def test_a_marker_naming_only_another_project_keeps_this_fast_path(
        self, repo: Path, graph: _Graph, shared: bool
    ) -> None:
        _sync(repo, graph, {1: _KEEP})
        if shared:
            _shared_marker(repo).write_text(json.dumps([_OTHER]), encoding="utf-8")
        else:
            _owe(repo, _OTHER)

        assert _in_sync(repo, graph) is True

    def test_a_sync_without_embeddings_never_writes_the_marker(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: not even when the store is down and an edit re-parses,
        # and another project's debt is left exactly as it was.
        with _store_down():
            _sync(repo, graph, {1: _KEEP}, skip_embeddings=True)
            assert _pending(repo) == set()
            _owe(repo, _OTHER)
            _shared_marker(repo).write_text(json.dumps([_OTHER]), encoding="utf-8")
            _sync(
                repo,
                graph,
                {2: _KEEP},
                source=_SOURCE_KEEP_EDITED,
                skip_embeddings=True,
            )

        assert _pending(repo) == {_OTHER}
        assert json.loads(_shared_marker(repo).read_text()) == [_OTHER]

    @pytest.mark.parametrize(
        "content",
        [json.dumps([_PROJECT, _OTHER]), "", "not json", '{"svc": true}', "[1]"],
    )
    def test_a_shared_marker_makes_each_project_it_names_retry_once(
        self, repo: Path, graph: _Graph, content: str
    ) -> None:
        # An earlier build's marker: the list of projects that owe, or, when
        # it names no one (its empty file), every project indexed from the
        # tree. svc's retry splits it, leaving other's debt in a file of its
        # own, and other's retry settles that.
        _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        _sync(repo, graph, {1: _KEEP})
        _shared_marker(repo).write_text(content, encoding="utf-8")
        assert _in_sync(repo, graph) is False

        assert _sync(repo, graph, {1: _KEEP}).skipped_because_in_sync is False

        assert not _shared_marker(repo).exists()
        assert _pending(repo) == {_OTHER}
        assert _in_sync(repo, graph) is True
        _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert _pending(repo) == set()


@contextmanager
def _no_room_for(repo: Path, project: str) -> Iterator[list[Path]]:
    """Fail the write of `project`'s marker as a full disk does, while its
    removal and every other file still go through. Yields the refused paths."""
    target = gu._embeddings_marker(state_file(repo, ""), project)
    real_write = Path.write_text
    refused: list[Path] = []

    def write_text(self: Path, data: str, *args: str, **kwargs: str) -> int:
        if self == target:
            refused.append(self)
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_write(self, data, *args, **kwargs)

    with patch.object(Path, "write_text", write_text):
        yield refused


class TestSplittingTheSharedMarker:
    """The shared marker goes only once every debt it holds has a file."""

    def _other_owes_through_the_shared_marker(self, repo: Path, graph: _Graph) -> None:
        # other's pass failed, recorded the way an earlier build did: in the
        # shared list. svc synced first, so other's stamp is the last one
        # and other's next unchanged sync is eligible for the fast path.
        _sync(repo, graph, {1: _KEEP})
        with _store_down():
            _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert _pending(repo) == {_OTHER}
        gu._embeddings_marker(state_file(repo, ""), _OTHER).unlink()
        _shared_marker(repo).write_text(json.dumps([_OTHER]), encoding="utf-8")

    def test_a_failed_write_keeps_the_shared_marker_and_the_retry(
        self, repo: Path, graph: _Graph
    ) -> None:
        self._other_owes_through_the_shared_marker(repo, graph)

        # A single-file run of svc leaves other's stamp and cache alone.
        with _no_room_for(repo, _OTHER) as refused:
            _sync(repo / _MODULE, graph, {1: _KEEP})
        assert refused

        assert _shared_marker(repo).exists()
        retry = _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert retry.skipped_because_in_sync is False
        assert (_OTHER_KEEP, 7) in _points()

    def test_a_failed_write_of_this_runs_own_debt_keeps_the_shared_marker(
        self, repo: Path, graph: _Graph
    ) -> None:
        # svc's own debt, listed in the shared marker, is lost just the same
        # if the marker goes and svc's own file could not be written.
        _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        _sync(repo, graph, {1: _KEEP})
        _shared_marker(repo).write_text(json.dumps([_PROJECT]), encoding="utf-8")

        (repo / _MODULE).write_text(_SOURCE_KEEP_EDITED)
        with _store_down(), _no_room_for(repo, _PROJECT) as refused:
            _sync(repo / _MODULE, graph, {2: _KEEP})
        assert refused

        assert _shared_marker(repo).exists()
        assert _sync(repo, graph, {2: _KEEP}).skipped_because_in_sync is False
        assert (_KEEP, 2) in _points()

    def test_the_next_sync_completes_a_split_a_failed_write_cut_short(
        self, repo: Path, graph: _Graph
    ) -> None:
        # Negative: once the write goes through, the split finishes and the
        # shared marker goes, as when every write succeeds the first time.
        self._other_owes_through_the_shared_marker(repo, graph)
        with _no_room_for(repo, _OTHER):
            _sync(repo / _MODULE, graph, {1: _KEEP})

        _sync(repo / _MODULE, graph, {1: _KEEP})

        assert not _shared_marker(repo).exists()
        assert _pending(repo) == {_OTHER}
        retry = _sync(repo, graph, {7: _OTHER_KEEP}, project=_OTHER)
        assert retry.skipped_because_in_sync is False
        assert _pending(repo) == set()

    def test_a_split_whose_writes_all_succeed_removes_the_shared_marker(
        self, repo: Path, graph: _Graph
    ) -> None:
        self._other_owes_through_the_shared_marker(repo, graph)

        _sync(repo / _MODULE, graph, {1: _KEEP})

        assert not _shared_marker(repo).exists()
        assert _pending(repo) == {_OTHER}


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
