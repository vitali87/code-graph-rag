"""A skipped EXPOSES cleanup is owed to the next run (issue #2193).

With the project registry unreadable, both EXPOSES cleanups skip rather than
delete rows the prefix fallback may not own. Nothing on disk changed, so the
next run would take the in-sync fast path and never do the cleanup; a marker
file now refuses that path until a batch run has done it.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_updater as gu
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import IngestorProtocol
from codebase_rag.types_defs import PropertyDict, PropertyValue
from evals.cgr_graph import _StatefulIngestor

ROUTES = '@app.get("/items")\ndef items():\n    return 1\n'
# EXPOSES is off by default (it belongs to the io group); without it neither
# endpoint pass reaches its cleanup.
EXPOSES_ON = resolve_capture([f"+{cs.RelationshipType.EXPOSES.value}"])


class _Recording(_StatefulIngestor):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []

    def execute_write(self, query: str, params: PropertyDict | None = None) -> None:
        self.writes.append(query)
        super().execute_write(query, params)


class _RegistryDown(_StatefulIngestor):
    def fetch_all(self, query, params=None):  # type: ignore[override]
        if query == cq.CYPHER_LIST_PROJECTS:
            raise RuntimeError("registry unreadable")
        return super().fetch_all(query, params)


def _updater(
    root: Path, store: IngestorProtocol, capture: CaptureSelection = EXPOSES_ON
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=capture,
    )


def test_a_skipped_cleanup_refuses_the_in_sync_path_until_done(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "api.py").write_text(ROUTES, encoding="utf-8")
    store = _Recording()
    _updater(root, store).run(force=True)
    assert _updater(root, store)._is_already_in_sync() is True
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    assert not marker.exists()

    down = _RegistryDown()
    down.__dict__.update(store.__dict__)
    blind = _updater(root, down)
    blind._registry_unread = True
    blind._drop_stale_handler_exposes(["proj.api.items"])
    blind._record_exposes_cleanup(cleared_by_this_run=True)
    assert marker.exists()
    # Record the root's mtime AFTER the marker was written, as the run that
    # skipped the cleanup does: the marker alone must refuse the fast path,
    # not the root's mtime moving.
    mtimes_path = state_file(root, cs.DIR_MTIMES_FILENAME)
    mtimes = gu._load_dir_mtimes(mtimes_path)
    mtimes[cs.ROOT_DIR_KEY] = os.stat(root).st_mtime
    gu._save_dir_mtimes(mtimes_path, mtimes)
    assert _updater(root, store)._is_already_in_sync() is False
    marker.unlink()
    assert _updater(root, store)._is_already_in_sync() is True, (
        "the control: with the marker gone the same tree is in sync"
    )
    marker.touch()
    mtimes[cs.ROOT_DIR_KEY] = os.stat(root).st_mtime
    gu._save_dir_mtimes(mtimes_path, mtimes)

    # A healthy batch run does the cleanups and settles what was owed. The
    # marker lives in the repository root, so removing it moves the root's
    # mtime once more; at most one further run is a no-op before the fast
    # path is back.
    store.writes.clear()
    _updater(root, store).run()
    assert gu.CYPHER_DELETE_HANDLER_EXPOSES in store.writes, (
        "the settling run must really have run the cleanup it settles"
    )
    assert not marker.exists()
    _updater(root, store).run()
    assert _updater(root, store)._is_already_in_sync() is True


def test_a_scoped_pass_never_settles_an_owed_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    marker.touch()
    updater = _updater(root, _StatefulIngestor())
    updater._record_exposes_cleanup(cleared_by_this_run=False)
    assert marker.exists()


class _DeletesFail(_StatefulIngestor):
    def execute_write(self, query, params=None):  # type: ignore[override]
        if query in (gu.CYPHER_DELETE_HANDLER_EXPOSES, gu.CYPHER_DELETE_MODULE_EXPOSES):
            raise RuntimeError("write refused")
        return super().execute_write(query, params)


def test_a_failed_cleanup_delete_is_owed_like_a_skipped_one(tmp_path: Path) -> None:
    # A delete that raises left the stale edges in place, and settling the
    # marker anyway let the next run take the in-sync path and keep them.
    for cleanup in ("_drop_stale_handler_exposes", "_drop_stale_module_exposes"):
        root = tmp_path / cleanup / "proj"
        root.mkdir(parents=True)
        marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
        marker.touch()
        updater = _updater(root, _DeletesFail())
        getattr(updater, cleanup)(["proj.api.items"])
        updater._record_exposes_cleanup(cleared_by_this_run=True)
        assert marker.exists(), cleanup


class _WriteOnly:
    # A sink with no query surface, like an export file: it can delete nothing.
    def ensure_node_batch(self, label: str, properties: PropertyDict) -> None:
        pass

    def ensure_relationship_batch(
        self,
        from_spec: tuple[str, str, PropertyValue],
        rel_type: str,
        to_spec: tuple[str, str, PropertyValue],
        properties: PropertyDict | None = None,
    ) -> None:
        pass

    def flush_all(self) -> None:
        pass


def test_a_run_that_cannot_clean_never_settles_an_owed_cleanup(tmp_path: Path) -> None:
    # With EXPOSES capture off both passes return before their cleanups, and
    # a sink that cannot query deletes nothing: neither did the cleanup, so
    # neither may report it done.
    root = tmp_path / "proj"
    root.mkdir()
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    marker.touch()
    for store, capture in (
        (_StatefulIngestor(), resolve_capture([])),
        (_WriteOnly(), EXPOSES_ON),
    ):
        updater = _updater(root, store, capture)
        updater._record_exposes_cleanup(cleared_by_this_run=True)
        assert marker.exists(), type(store).__name__


def test_an_owed_cleanup_costs_no_rescan_while_exposes_is_off(tmp_path: Path) -> None:
    # Kept, the marker would otherwise send every run with EXPOSES off into
    # passes that can never settle it.
    root = tmp_path / "proj"
    root.mkdir()
    (root / "api.py").write_text(ROUTES, encoding="utf-8")
    off = resolve_capture([])
    store = _StatefulIngestor()
    _updater(root, store, off).run(force=True)
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    marker.touch()
    assert _updater(root, store, off)._is_already_in_sync() is True
    assert _updater(root, store, EXPOSES_ON)._sync_state_forces_reparse() is True, (
        "the control: a run that can clean is still sent to do it"
    )


_REAL_TOUCH = Path.touch
_REAL_UNLINK = Path.unlink


def _touch_refusing(target: Path) -> Callable[[Path, int, bool], None]:
    def touch(self: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
        if self == target:
            raise PermissionError("read-only")
        _REAL_TOUCH(self, mode, exist_ok)

    return touch


def _unlink_refusing(target: Path) -> Callable[[Path, bool], None]:
    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self == target:
            raise PermissionError("read-only")
        _REAL_UNLINK(self, missing_ok)

    return unlink


def test_an_unwritable_marker_leaves_no_snapshot_to_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without the marker nothing on disk owed the cleanup, and a run that
    # committed its caches let the next unchanged run take the in-sync path.
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    api = root / "pkg" / "api.py"
    api.write_text(ROUTES, encoding="utf-8")
    store = _Recording()
    _updater(root, store).run(force=True)
    cache = state_file(root, cs.HASH_CACHE_FILENAME)
    snapshot = state_file(root, cs.DIR_MTIMES_FILENAME)
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    assert cache.is_file()
    assert snapshot.is_file()

    # A context, not `monkeypatch.undo()`: that would also undo the isolated
    # CGR_HOME the suite's own fixtures set through the same monkeypatch.
    with monkeypatch.context() as patch:
        patch.setattr(Path, "touch", _touch_refusing(marker))
        api.write_text(ROUTES + "\n", encoding="utf-8")
        down = _RegistryDown()
        down.__dict__.update(store.__dict__)
        blind = _updater(root, down)
        blind.run()
    assert blind._exposes_cleanup_skipped, "the run must have skipped a cleanup"
    assert not marker.exists()
    assert not snapshot.exists(), "a committed map would hide the owed cleanup"
    assert _updater(root, store)._is_already_in_sync() is False
    # The hash cache stays: a single-file run finds its project root by it,
    # and without it `pkg/api.py` would root at `pkg` (Greptile, PR #3625).
    assert cache.is_file()
    assert gu._project_root_for_single_file(api.resolve()) == root.resolve()

    store.writes.clear()
    _updater(root, store).run()
    assert gu.CYPHER_DELETE_HANDLER_EXPOSES in store.writes
    assert snapshot.is_file()


def test_a_kept_map_holds_the_hash_cache_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the marker unwritten and the old map kept, a fresh cache made the
    # files this run indexed read as unchanged against that map, and the next
    # run took the in-sync path past the owed cleanup.
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    api = root / "pkg" / "api.py"
    api.write_text(ROUTES, encoding="utf-8")
    store = _Recording()
    _updater(root, store).run(force=True)
    cache = state_file(root, cs.HASH_CACHE_FILENAME)
    snapshot = state_file(root, cs.DIR_MTIMES_FILENAME)
    marker = state_file(root, cs.EXPOSES_CLEANUP_PENDING_FILENAME)
    indexed = cache.read_text(encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "touch", _touch_refusing(marker))
        patch.setattr(Path, "unlink", _unlink_refusing(snapshot))
        api.write_text(ROUTES + "\n", encoding="utf-8")
        down = _RegistryDown()
        down.__dict__.update(store.__dict__)
        blind = _updater(root, down)
        blind.run()
    assert blind._exposes_cleanup_skipped, "the run must have skipped a cleanup"
    assert snapshot.is_file()
    assert cache.read_text(encoding="utf-8") == indexed
    assert _updater(root, store)._is_already_in_sync() is False

    # A scoped pass holds its hash update back the same way.
    scoped = _updater(root, store)
    scoped._exposes_map_kept = True
    scoped._reingest_update_hashes(
        cache,
        gu._load_hash_cache(cache),
        {"pkg/api.py": api},
        {"pkg/api.py": api.read_bytes()},
        {},
    )
    assert cache.read_text(encoding="utf-8") == indexed
