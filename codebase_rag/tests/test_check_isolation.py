"""`cgr check --isolated` measures the working tree without keeping the re-ingest.

Issue #1718. The check re-ingests the files that differ from the base, which
brings the shared graph up to the working tree: a second run on the same edit
reports nothing, and a prior check makes a later `--fail-on-found` pass for
the same edits. Isolated mode captures the subgraph the re-ingest is about to
replace, runs the same check, and puts the capture back, so the graph and the
on-disk hash cache read exactly as they did before, and the check can be
rerun.

The store is the eval emulator, which models the module-subtree delete and
the capture queries by value; the same edit is replayed against a real
Memgraph in the integration tier.
"""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.check_isolation import IsolationGuard
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import run_check
from codebase_rag.structural_delta import StructuralDelta
from evals.cgr_graph import _StatefulIngestor

PROJECT = "iso_fixture"

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a):\n    return a + 1\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 2\n"
    ),
}

_TIMINGS = ("reingest_ms", "delta_ms")


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return root, store


def _updater(
    store: _StatefulIngestor, root: Path, capture: CaptureSelection | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        capture=capture,
    )


def _state(store: _StatefulIngestor) -> tuple[dict, set, dict]:
    return (
        copy.deepcopy(store.nodes),
        set(store.edges),
        copy.deepcopy(store.edge_props),
    )


def _check(root: Path, store: _StatefulIngestor, *, isolated: bool) -> StructuralDelta:
    parsers, queries = load_parsers()
    return run_check(root, "HEAD", PROJECT, store, parsers, queries, isolated=isolated)


def _findings(delta: StructuralDelta) -> dict[str, Any]:
    return {key: value for key, value in delta.items() if key not in _TIMINGS}


def _edit(root: Path) -> None:
    """One edit of every kind the re-ingest handles differently.

    A renamed definition (its callers dangle), a deleted module, a new
    module in a new directory (a Folder the graph has never seen), a new
    external import, and a deleted package indicator (`tests/` flips from
    Package to Folder).
    """
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    (root / "main.py").unlink()
    _write(root, "lib/tool.py", "import os\n\n\ndef tool():\n    return os.sep\n")
    (root / "tests" / "__init__.py").unlink()


def _labelled(store: _StatefulIngestor, label: str) -> set[Any]:
    return {uid for (node_label, uid) in store.nodes if node_label == label}


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


# --- the report ---------------------------------------------------------------


def test_an_isolated_check_reports_what_an_applied_check_reports(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)
    twin = copy.deepcopy(store)

    isolated = _check(root, store, isolated=True)
    applied = _check(root, twin, isolated=False)

    assert _findings(isolated) == _findings(applied)
    assert isolated["dangling_callers"][0]["target"] == f"{PROJECT}.pkg.util.helper"
    assert f"{PROJECT}.main.main" in isolated["symbols"]["removed"]


def test_an_isolated_check_reports_the_same_delta_when_rerun(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    _edit(root)

    first = _check(root, store, isolated=True)
    second = _check(root, store, isolated=True)

    assert _findings(second) == _findings(first)
    assert second["dangling_callers"]


def test_an_applied_check_reports_nothing_the_second_time(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The contract the issue describes, kept as the control."""
    root, store = indexed
    _edit(root)

    first = _check(root, store, isolated=False)
    second = _check(root, store, isolated=False)

    assert first["dangling_callers"]
    assert not second["dangling_callers"]
    assert not second["symbols"]["removed"]


# --- the disk -----------------------------------------------------------------


def test_an_isolated_check_restores_the_hash_cache(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The re-ingest records the re-parsed files as indexed in the hash
    cache; a later full run would then skip them and keep the base graph
    for files the working tree changed."""
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    assert cache.is_file()
    _edit(root)
    content = cache.read_bytes()
    mtime = cache.stat().st_mtime_ns

    _check(root, store, isolated=True)

    assert cache.read_bytes() == content
    assert cache.stat().st_mtime_ns == mtime


def test_an_applied_check_rewrites_the_hash_cache(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    root, store = indexed
    cache = root / cs.HASH_CACHE_FILENAME
    _edit(root)
    content = cache.read_bytes()

    _check(root, store, isolated=False)

    assert cache.read_bytes() != content


# --- per-site edges -----------------------------------------------------------


def test_two_sites_on_one_pair_are_captured_and_restored_separately() -> None:
    """Parallel edges differ only in their site properties.

    `MERGE_KEY_PROPS_BY_REL` makes `(line, col)` part of a CALLS edge's
    identity, so one caller/callee pair carries one edge per site (#1522).
    The guard must key its capture the same way or the second site's row
    replaces the first and the restore re-emits one edge where there were
    two.

    Driven against a hand-built store rather than a parsed fixture: the
    eval double collapses parallel edges into one (issue #1921), so a
    fixture with two call sites cannot express the case it is meant to
    test. The rows here are the shape the production query returns.
    """
    captured: list[tuple[object, str, object, dict | None]] = []

    class _Store:
        def fetch_all(self, query: str, params: dict | None = None) -> list[dict]:
            if query == cq.CYPHER_CHECK_SCOPE_NODES:
                return []
            return [
                {
                    cs.KEY_LABEL: cs.NodeLabel.FUNCTION.value,
                    cs.KEY_QUALIFIED_NAME: "p.app.run",
                    cs.KEY_REL: cs.RelationshipType.CALLS.value,
                    cs.KEY_OUTGOING: True,
                    cs.KEY_PROPS: {cs.KEY_LINE: 5, cs.KEY_COL: col},
                    cs.KEY_FAR_LABEL: cs.NodeLabel.FUNCTION.value,
                    cs.FAR_END_PREFIX + cs.KEY_QUALIFIED_NAME: "p.util.helper",
                }
                for col in (11, 23)
            ]

        def execute_write(self, query: str, params: dict | None = None) -> None:
            return None

        def ensure_node_batch(self, label: str, properties: dict) -> None:
            return None

        def ensure_relationship_batch(
            self,
            from_spec: tuple,
            rel_type: str,
            to_spec: tuple,
            properties: dict | None = None,
        ) -> None:
            captured.append((from_spec, rel_type, to_spec, properties))

        def flush_all(self) -> None:
            return None

    guard = IsolationGuard(_Store(), "p", Path("/nonexistent"))
    guard.capture(["app.py"])
    guard.restore()

    assert [props[cs.KEY_COL] for _s, _r, _t, props in captured if props] == [11, 23]


# --- the updater's side of the contract ---------------------------------------


def test_reingest_names_its_scope_before_the_write_hook_runs(
    indexed: tuple[Path, _StatefulIngestor],
) -> None:
    """The capture has to cover exactly what the re-ingest deletes: the
    changed files plus the dependents it re-parses, known only inside the
    prologue. The updater publishes that set right before `before_write`."""
    root, store = indexed
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("helper", "assist"))
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    assert updater.reingest_scope == ()
    seen: list[tuple[str, ...]] = []

    updater.reingest(
        ["pkg/util.py"], before_write=lambda: seen.append(updater.reingest_scope)
    )

    assert seen == [("pkg/app.py", "pkg/util.py")]
