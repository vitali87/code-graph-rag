"""Folders of a newly excluded directory leave the graph with its files.

Excluding a directory after a project is indexed (`.cgrignore`, `--exclude`)
removed its Modules and Files on the next sync (#1606/#1621), but its Folder
nodes stayed: the prune treated a Folder as stale only when its directory was
gone from disk, so the graph depended on its history and a fresh index of
the same tree disagreed with it (issue #2884).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_FILES = (
    "src/pkg/a.py",
    "tests/test_a.py",
    "tests/sub/x.py",
    "examples/ex1/app.py",
    "examples/ex1/inner/y.py",
    "examples/ex2/app.py",
)
# The issue's `.cgrignore`.
_EXCLUDE = frozenset({"tests", "examples/**"})


def _tree(root: Path) -> Path:
    for rel in _FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def f():\n    return 1\n", encoding="utf-8")
    return root


def _sync(store: _StatefulIngestor, root: Path, exclude: frozenset[str]) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="demo",
        exclude_paths=exclude,
    ).run()


def _paths(store: _StatefulIngestor, label: cs.NodeLabel) -> set[str]:
    return {
        str(props[cs.KEY_PATH])
        for (node_label, _key), props in store.nodes.items()
        if node_label == label
    }


def _folder_edges(store: _StatefulIngestor) -> set[str]:
    return {
        str(edge[4])
        for edge in store.edges
        if edge[2] == cs.RelationshipType.CONTAINS_FOLDER
    }


def _fresh(tmp_path: Path) -> _StatefulIngestor:
    store = _StatefulIngestor()
    _sync(store, _tree(tmp_path / "fresh" / "proj"), _EXCLUDE)
    return store


def test_newly_excluded_folders_are_pruned(tmp_path: Path) -> None:
    root = _tree(tmp_path / "synced" / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    assert "tests/sub" in _paths(store, cs.NodeLabel.FOLDER)

    _sync(store, root, _EXCLUDE)

    fresh = _fresh(tmp_path)
    assert _paths(store, cs.NodeLabel.FOLDER) == _paths(fresh, cs.NodeLabel.FOLDER)
    assert _paths(store, cs.NodeLabel.FOLDER) == {"src", "src/pkg"}
    assert len(_folder_edges(store)) == len(_folder_edges(fresh))


def test_a_folder_back_in_scope_returns(tmp_path: Path) -> None:
    # Negative: lifting the exclusion brings the Folders back, so the prune
    # removed nothing a later sync cannot restore.
    root = _tree(tmp_path / "proj")
    store = _StatefulIngestor()
    _sync(store, root, _EXCLUDE)
    _sync(store, root, frozenset())
    assert {"tests", "tests/sub", "examples/ex1/inner"} <= _paths(
        store, cs.NodeLabel.FOLDER
    )


def test_a_deleted_folder_is_still_pruned(tmp_path: Path) -> None:
    # Negative: the on-disk rule that already worked keeps working.
    root = _tree(tmp_path / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    shutil.rmtree(root / "tests" / "sub")
    _sync(store, root, frozenset())
    folders = _paths(store, cs.NodeLabel.FOLDER)
    assert "tests/sub" not in folders, folders
    assert "tests" in folders, folders


@pytest.mark.parametrize("label", [cs.NodeLabel.MODULE, cs.NodeLabel.FILE])
def test_files_and_modules_still_match_a_fresh_index(
    tmp_path: Path, label: cs.NodeLabel
) -> None:
    # Negative: the #1621 path for files and modules is unchanged.
    root = _tree(tmp_path / "synced" / "proj")
    store = _StatefulIngestor()
    _sync(store, root, frozenset())
    _sync(store, root, _EXCLUDE)
    assert _paths(store, label) == _paths(_fresh(tmp_path), label)
