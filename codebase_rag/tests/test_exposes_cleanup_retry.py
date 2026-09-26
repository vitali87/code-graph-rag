"""A skipped EXPOSES cleanup is owed to the next run (issue #2193).

With the project registry unreadable, both EXPOSES cleanups skip rather than
delete rows the prefix fallback may not own. Nothing on disk changed, so the
next run would take the in-sync fast path and never do the cleanup; a marker
file now refuses that path until a batch run has done it.
"""

from __future__ import annotations

import os
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

ROUTES = '@app.get("/items")\ndef items():\n    return 1\n'


class _RegistryDown(_StatefulIngestor):
    def fetch_all(self, query, params=None):  # type: ignore[override]
        if query == cq.CYPHER_LIST_PROJECTS:
            raise RuntimeError("registry unreadable")
        return super().fetch_all(query, params)


def _updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store, repo_path=root, parsers=parsers, queries=queries
    )


def test_a_skipped_cleanup_refuses_the_in_sync_path_until_done(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "api.py").write_text(ROUTES, encoding="utf-8")
    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    assert _updater(root, store)._is_already_in_sync() is True
    marker = root / cs.EXPOSES_CLEANUP_PENDING_FILENAME
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
    mtimes_path = root / cs.DIR_MTIMES_FILENAME
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
    _updater(root, store).run()
    assert not marker.exists()
    _updater(root, store).run()
    assert _updater(root, store)._is_already_in_sync() is True


def test_a_scoped_pass_never_settles_an_owed_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    marker = root / cs.EXPOSES_CLEANUP_PENDING_FILENAME
    marker.touch()
    updater = _updater(root, _StatefulIngestor())
    updater._record_exposes_cleanup(cleared_by_this_run=False)
    assert marker.exists()
