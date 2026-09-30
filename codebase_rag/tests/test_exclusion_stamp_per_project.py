"""Each project indexed from one tree keeps its own exclusion stamp (#1987).

The stamp file's top level is the LAST run's, which is what the shared hash
cache belongs to; a per-project map beside it keeps every project's scope
readable by `cgr check`. Raised by CodeRabbit on PR #2251.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
from codebase_rag.graph_updater import GraphUpdater, _load_exclusion_state
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import indexed_scope
from codebase_rag.utils.path_utils import derive_project_name
from evals.cgr_graph import _StatefulIngestor

_SOURCE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a\n",
    "pkg/main.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
}


@pytest.fixture
def root(temp_repo: Path) -> Path:
    tree = temp_repo / "tree"
    for rel, text in _SOURCE.items():
        path = tree / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tree


def _updater(
    repo_path: Path,
    store: _StatefulIngestor,
    project_name: str,
    *,
    named: bool | None = None,
    exclude: frozenset[str] | None = None,
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
        project_name=project_name,
        project_named=named,
        exclude_paths=exclude,
    )


def test_check_reads_every_projects_own_scope_after_both_indexed(root: Path) -> None:
    store = _StatefulIngestor()
    _updater(root, store, "project_a", exclude=frozenset({"gen_a"})).run(force=True)
    _updater(root, store, "project_b", exclude=frozenset({"gen_b"})).run(force=True)

    assert indexed_scope(root, "project_a", explicit=True) == (
        frozenset({"gen_a"}),
        None,
    )
    assert indexed_scope(root, "project_b", explicit=True) == (
        frozenset({"gen_b"}),
        None,
    )
    # The top level stays the last run's: the hash cache is that project's.
    top = _load_exclusion_state(state_file(root, cs.EXCLUSION_STATE_FILENAME))
    assert top is not None
    assert top["project"] == "project_b"


def test_an_in_sync_named_run_refreshes_an_unnamed_stamp(root: Path) -> None:
    # A derived name first indexed unnamed, then synced under the same name
    # given explicitly: the scope matches, so the run takes the in-sync fast
    # path, which used to return before the stamp was written and left it
    # unnamed -- `cgr check --project <name>` then refused.
    store = _StatefulIngestor()
    name = derive_project_name(root)
    _updater(root, store, name, named=False).run(force=True)

    second = _updater(root, store, name, named=True)
    second.run()

    assert second.skipped_because_in_sync is True
    assert indexed_scope(root, name, explicit=True) == (None, None)


def test_an_unchanged_in_sync_run_does_not_rewrite_the_stamp(root: Path) -> None:
    store = _StatefulIngestor()
    _updater(root, store, "project_a").run(force=True)
    stamp = state_file(root, cs.EXCLUSION_STATE_FILENAME)
    before = stamp.read_bytes()
    stamp.write_bytes(before)
    written_at = stamp.stat().st_mtime_ns

    second = _updater(root, store, "project_a")
    second.run()

    assert second.skipped_because_in_sync is True
    assert stamp.stat().st_mtime_ns == written_at
    assert stamp.read_bytes() == before


def test_a_single_file_run_does_not_publish_into_another_projects_cache(
    root: Path,
) -> None:
    # project_a indexed first, then project_b took the stamp and the cache.
    # A single-file run for project_a must not merge its hash into
    # project_b's cache, or project_b's next sync reads the edited file as
    # current while its graph still holds the old parse.
    store = _StatefulIngestor()
    _updater(root, store, "project_a").run(force=True)
    _updater(root, store, "project_b").run(force=True)
    cache = state_file(root, cs.HASH_CACHE_FILENAME)
    before = json.loads(cache.read_text(encoding="utf-8"))

    target = root / "pkg" / "util.py"
    target.write_text("def helper(a):\n    return a + 1\n", encoding="utf-8")
    _updater(target, store, "project_a").run()

    assert json.loads(cache.read_text(encoding="utf-8")) == before
