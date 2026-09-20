from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from check_isolation_helpers import (
    PROJECT,
    _check,
    _edit,
    _labelled,
    _state,
    _updater,
    _write,
)

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.check_isolation import IsolationGuard
from codebase_rag.graph_updater import GraphUpdater
from evals.cgr_graph import _StatefulIngestor

pytest_plugins = ("check_isolation_helpers",)

# --- the graph after the check ------------------------------------------------


def test_an_isolated_check_leaves_every_node_and_edge_as_it_was(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)
    before = _state(store)

    _check(root, store, isolated=True)

    assert _state(store) == before


def test_an_applied_check_changes_the_graph(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The control for the test above: without isolation the edit lands."""
    root, store = indexed
    _edit(root)
    before = _state(store)

    _check(root, store, isolated=False)

    assert _state(store) != before
    assert (cs.NodeLabel.MODULE.value, f"{PROJECT}.main") not in store.nodes


def test_an_isolated_check_restores_a_flipped_container(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """`tests/` loses its `__init__.py`: the check writes a Folder and prunes
    the Package; the restore must do the opposite, edges included."""
    root, store = indexed
    (root / "tests" / "__init__.py").unlink()
    tests_dir = (root / "tests").resolve().as_posix()
    packages_before = _labelled(store, cs.NodeLabel.PACKAGE.value)
    assert f"{PROJECT}.tests" in packages_before
    folders_before = _labelled(store, cs.NodeLabel.FOLDER.value)
    assert tests_dir not in folders_before
    before = _state(store)

    _check(root, store, isolated=True)

    assert _labelled(store, cs.NodeLabel.FOLDER.value) == folders_before
    assert _labelled(store, cs.NodeLabel.PACKAGE.value) == packages_before
    assert _state(store) == before


def test_an_isolated_check_drops_a_directory_the_graph_never_had(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _write(root, "lib/tool.py", "def tool():\n    return 1\n")
    lib_dir = (root / "lib").resolve().as_posix()
    before = _state(store)

    _check(root, store, isolated=True)

    assert lib_dir not in _labelled(store, cs.NodeLabel.FOLDER.value)
    assert (root / "lib" / "tool.py").resolve().as_posix() not in _labelled(
        store, cs.NodeLabel.FILE.value
    )
    assert _state(store) == before


def test_nodes_the_reingest_creates_beside_the_subtree_do_not_outlive_the_restore(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """An ExternalModule for a new import and a CodeSmell for a new match
    hang off the module without being DEFINED by it, so the subtree delete
    alone would leave them behind. Findings are an opt-in capture group the
    check itself never enables, so the guard is driven directly here with a
    re-ingest that has them on; the mid-way assertions are the known
    positive for the absences asserted after the restore."""
    pytest.importorskip("ast_grep_py")
    root, store = indexed
    _write(root, "pkg/app.py", "import os\n\n\ndef run(x=[]):\n    return os.sep\n")
    assert "os" not in _labelled(store, cs.NodeLabel.EXTERNAL_MODULE.value)
    assert not _labelled(store, cs.NodeLabel.CODE_SMELL.value)
    before = _state(store)
    updater = _updater(store, root, capture=resolve_capture(["findings"]))
    guard = IsolationGuard(store, PROJECT, root)

    updater.reingest(
        ["pkg/app.py"], before_write=lambda: guard.capture(updater.reingest_scope)
    )
    assert "os" in _labelled(store, cs.NodeLabel.EXTERNAL_MODULE.value)
    assert _labelled(store, cs.NodeLabel.CODE_SMELL.value)

    guard.restore()

    assert "os" not in _labelled(store, cs.NodeLabel.EXTERNAL_MODULE.value)
    assert not _labelled(store, cs.NodeLabel.CODE_SMELL.value)
    assert _state(store) == before


def test_a_failure_during_the_reingest_still_restores_the_graph(
    indexed: tuple[Path, _StatefulIngestor], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subtrees are already deleted when the re-parse runs; a failure
    there must not leave the graph half-written under the isolated flag."""
    root, store = indexed
    _edit(root)
    before = _state(store)

    def boom(self: GraphUpdater, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(GraphUpdater, "_reingest_reparse", boom)
    with pytest.raises(RuntimeError, match="parser exploded"):
        _check(root, store, isolated=True)

    assert _state(store) == before
