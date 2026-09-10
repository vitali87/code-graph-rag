"""A directory's package-ness must be re-evaluated by `reingest` too (#1798).

Adding or deleting an `__init__.py` changes what a directory IS: with one it
is a `Package` keyed on a dotted qualified name, without one a `Folder` keyed
on an absolute path. Every `CONTAINS_FILE` and `CONTAINS_MODULE` edge under it
hangs off whichever node it is.

`run()` handles this (issue #1570): it captures the package set before Pass 1,
computes the symmetric difference in `_package_flip_dirs`, re-parses the
flipped directory's siblings so their containment edges re-point, and prunes
the node of the wrong kind in `_prune_orphan_nodes`.

The scoped `reingest` path did none of that. It never captured the before-set,
never computed the flip, never re-parsed the siblings, and never pruned -- so
the directory kept its old identity and every edge stayed anchored to it.

Both DIRECTIONS are tested. The issue reports the demotion (deleting
`__init__.py`), but the promotion is broken identically and is arguably the
more common event: creating a package means creating the directory and its
`__init__.py` in quick succession, so the watcher may well see the directory
as a `Folder` first and never correct it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

UTIL = "def helper():\n    return 1\n"


def _fixture(root: Path, *, with_init: bool) -> None:
    (root / "pkg").mkdir(parents=True)
    if with_init:
        (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(UTIL, encoding="utf-8")


def _updater(root: Path, store: _StatefulIngestor) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _containers(store: _StatefulIngestor) -> set[tuple[str, str]]:
    """Package and Folder nodes, with paths reduced to their last segment.

    A Folder is keyed on an absolute path and a Package on a dotted qn, so the
    raw uids are not comparable between two different temp roots. The last
    segment is what distinguishes them here, and keeping the LABEL is what the
    assertion is actually about.
    """
    return {
        (label, str(uid).split("/")[-1])
        for (label, uid) in store.nodes
        if label in (cs.NodeLabel.PACKAGE.value, cs.NodeLabel.FOLDER.value)
    }


def _containment(store: _StatefulIngestor) -> set[tuple[str, str, str]]:
    return {
        (str(src).split("/")[-1], rel, str(dst).split("/")[-1])
        for (_sl, src, rel, _tl, dst) in store.edges
        if rel.startswith("CONTAINS")
    }


def _clean_index(root: Path, *, with_init: bool) -> _StatefulIngestor:
    _fixture(root, with_init=with_init)
    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()
    return store


@pytest.mark.parametrize("direction", ["demote", "promote"])
def test_reingest_re_evaluates_a_directorys_package_ness(
    tmp_path: Path, direction: str
) -> None:
    """The incremental result must match a clean index of the same tree.

    Asserted as a comparison against a real rebuild rather than against a
    hardcoded label, because the node IDENTITY changes with the kind -- a
    Package is keyed on `proj.pkg`, a Folder on an absolute path -- so an
    assertion naming one expected uid would pin the wrong thing and would not
    notice the containment edges staying behind.
    """
    started_as_package = direction == "demote"
    root = tmp_path / "incremental"
    root.mkdir()
    _fixture(root, with_init=started_as_package)

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()

    init = root / "pkg" / "__init__.py"
    if started_as_package:
        assert ("Package", "proj.pkg") in _containers(store), (
            "fixture guard: the initial index must produce a Package, or the "
            "demotion below has nothing to demote"
        )
        init.unlink()
        updater.reingest([], deleted=["pkg/__init__.py"])
    else:
        assert ("Folder", "pkg") in _containers(store), (
            "fixture guard: the initial index must produce a Folder, or the "
            "promotion below has nothing to promote"
        )
        init.write_text("", encoding="utf-8")
        updater.reingest(["pkg/__init__.py"])
    store.flush_all()

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    clean = _clean_index(clean_root, with_init=not started_as_package)

    assert _containers(store) == _containers(clean), (
        f"after a {direction}, the directory's kind disagrees with a clean "
        f"index: incremental={sorted(_containers(store))} "
        f"clean={sorted(_containers(clean))}"
    )
    assert _containment(store) == _containment(clean), (
        "the directory's kind changed but its containment edges did not "
        f"move with it: incremental={sorted(_containment(store))} "
        f"clean={sorted(_containment(clean))}"
    )


def test_an_unrelated_reingest_does_not_disturb_a_package(tmp_path: Path) -> None:
    """The control against re-evaluating too eagerly.

    A fix that re-derived every directory's kind on every scoped re-ingest, or
    that pruned whenever it could not prove a directory was still a package,
    would satisfy both cases above while churning nodes on ordinary edits.
    Re-parsing a file that is not an `__init__.py` must leave the container
    exactly as it was.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _fixture(root, with_init=True)

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    before = _containers(store)
    assert ("Package", "proj.pkg") in before, "fixture guard: expected a Package"

    (root / "pkg" / "util.py").write_text("def helper():\n    return 2\n", "utf-8")
    updater.reingest(["pkg/util.py"])
    store.flush_all()

    assert _containers(store) == before, (
        "an ordinary edit changed the containing directory's kind: "
        f"{sorted(before)} -> {sorted(_containers(store))}"
    )


def test_a_flip_elsewhere_is_not_this_calls_to_reconcile(tmp_path: Path) -> None:
    """The narrowing to directories this call actually touched.

    `before ^ after` finds every directory whose package-ness differs from the
    last derivation, which on a reused updater includes directories changed by
    something else entirely. Reconciling those would make a scoped re-ingest
    unbounded: it would re-parse and prune directories the caller never named,
    which is the whole-project claim `_prune_orphan_nodes` is careful not to
    make from a partial walk.

    Here `other/` loses its `__init__.py` on disk without being named in the
    call, and the re-ingest of `pkg/` must leave it alone. A full index will
    reconcile it; a scoped call that walked two files must not.

    Without the `& touched` narrowing this test goes red, and the tests above
    stay green -- over-reach is invisible to them, because they only assert
    that the touched directory IS reconciled.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _fixture(root, with_init=True)
    (root / "other").mkdir()
    (root / "other" / "__init__.py").write_text("", encoding="utf-8")
    (root / "other" / "mod.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    assert ("Package", "proj.other") in _containers(store), (
        "fixture guard: other/ must start as a Package"
    )

    # Someone else's change, on disk, not named in the call below.
    (root / "other" / "__init__.py").unlink()

    (root / "pkg" / "util.py").write_text("def helper():\n    return 2\n", "utf-8")
    updater.reingest(["pkg/util.py"])
    store.flush_all()

    assert ("Folder", "other") not in _containers(store), (
        "a scoped re-ingest of pkg/ demoted an unrelated directory whose "
        "__init__.py was removed by something else, so it acted on a "
        "whole-project claim it had not earned from walking two files"
    )
    assert ("Package", "proj.other") in _containers(store), (
        "the unrelated directory's Package node was pruned by a call that "
        "never named it"
    )


def test_a_repo_root_that_stops_being_a_package_is_demoted(tmp_path: Path) -> None:
    """The root is the one directory `identify_structure` treats specially.

    It emits no Folder node for the repo root -- the root's parent is the
    Project -- and the branch that skipped that emission also skipped
    RECORDING the root as no longer a package. So the root's stale qn survived
    a re-derivation, the flip was invisible, and its Package node was never
    pruned (greptile-local, #1798).

    Promotion needs no equivalent test: nothing stale exists when a root
    becomes a package for the first time.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "util.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    assert ("Package", "proj") in _containers(store), (
        "fixture guard: a root with an __init__.py must index as a Package"
    )

    (root / "__init__.py").unlink()
    updater.reingest([], deleted=["__init__.py"])
    store.flush_all()

    assert ("Package", "proj") not in _containers(store), (
        "the repo root stopped being a package and its Package node survived, "
        f"so every module still hangs off it: {sorted(_containers(store))}"
    )


def test_an_aborted_reingest_leaves_the_graph_as_it_was(tmp_path: Path) -> None:
    """The prologue must stay read-only, as `reingest`'s docstring promises.

    Deriving the structure to detect the flip EMITS the container node of the
    new kind. Doing that in the prologue meant a caller refusing in
    `before_write` left the graph holding BOTH container nodes for one
    directory -- while `reingest_mutated` stayed False, telling that caller
    nothing had changed. Base `main` leaves the graph untouched here, so this
    was a regression introduced by the fix, not a pre-existing gap
    (greptile-local, #1798).

    The detection is now a pure filesystem read and the derivation happens
    after the caller's last word.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _fixture(root, with_init=True)

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    before = _containers(store)

    (root / "pkg" / "__init__.py").unlink()

    def refuse() -> None:
        raise RuntimeError("the caller declined to proceed")

    with pytest.raises(Exception, match="declined"):
        updater.reingest([], deleted=["pkg/__init__.py"], before_write=refuse)
    store.flush_all()

    assert _containers(store) == before, (
        "an aborted re-ingest changed the graph it promised to leave alone: "
        f"{sorted(before)} -> {sorted(_containers(store))}"
    )
    assert updater.reingest_mutated is False, (
        "the abort reported that nothing changed, which must remain true"
    )


def test_a_named_flip_does_not_drag_in_an_unnamed_one(tmp_path: Path) -> None:
    """The `& touched` narrowing, on the case that actually reaches it.

    My first control named no indicator in its call, so the `if not touched`
    early return shielded it and removing the narrowing left it green -- I
    read that as defence in depth and was wrong. Here `pkg/__init__.py` IS
    named, so `touched` is non-empty and the narrowing is the only thing
    stopping an unrelated `other/` (changed on disk by something else) from
    being reconciled by a call that never mentioned it (greptile-local,
    #1798).
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _fixture(root, with_init=False)
    (root / "other").mkdir()
    (root / "other" / "__init__.py").write_text("", encoding="utf-8")
    (root / "other" / "mod.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    assert ("Package", "proj.other") in _containers(store), (
        "fixture guard: other/ must start as a Package"
    )

    # Someone else's change, on disk, never named below.
    (root / "other" / "__init__.py").unlink()
    # This call's own change, which legitimately flips pkg/.
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    updater.reingest(["pkg/__init__.py"])
    store.flush_all()

    assert ("Package", "proj.pkg") in _containers(store), (
        "fixture guard: the named directory must actually have been promoted"
    )
    assert ("Package", "proj.other") in _containers(store), (
        "a re-ingest that named pkg/ also reconciled other/, acting on a "
        "whole-project claim it had not earned from walking one file"
    )
    # The half this test originally missed. Asserting the Package SURVIVES
    # says nothing about whether a Folder was added beside it: the structure
    # derivation walks every directory and emits a node for each, so an
    # unrelated directory ended up with BOTH identities at once -- worse than
    # either reconciling it or leaving it alone (Greptile, PR #1835).
    assert ("Folder", "other") not in _containers(store), (
        "the unrelated directory gained a second container identity: it is "
        f"now both a Package and a Folder: {sorted(_containers(store))}"
    )


def test_a_nested_directory_flips_under_its_parent_package(tmp_path: Path) -> None:
    """The case the scoped derivation's ancestors exist for.

    Restricting the structure walk to the flipped directories stops an
    unrelated one gaining a second identity, but each directory's parent
    lookup reads the enclosing package's entry -- so a nested flip needs its
    ancestors in scope or it hangs off the wrong container.

    Honest caveat: removing the ancestor walk leaves THIS test green too.
    `structural_elements` persists across calls, so an ancestor derived by an
    earlier run is still in the map. The ancestor scope is defensive against a
    first derivation over an empty map; what this test genuinely pins is that
    a nested flip matches a clean index, which the scope narrowing could
    otherwise have broken.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "sub").mkdir()
    (root / "pkg" / "sub" / "mod.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    updater = _updater(root, store)
    updater.run(force=True)
    store.flush_all()
    assert ("Folder", "sub") in _containers(store), (
        "fixture guard: sub/ must start as a Folder inside a Package"
    )

    (root / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    updater.reingest(["pkg/sub/__init__.py"])
    store.flush_all()

    clean_root = tmp_path / "clean"
    clean_root.mkdir()
    (clean_root / "pkg").mkdir()
    (clean_root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (clean_root / "pkg" / "sub").mkdir()
    (clean_root / "pkg" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (clean_root / "pkg" / "sub" / "mod.py").write_text(UTIL, encoding="utf-8")
    clean_store = _StatefulIngestor()
    _updater(clean_root, clean_store).run(force=True)
    clean_store.flush_all()

    assert _containers(store) == _containers(clean_store), (
        "a nested promotion disagrees with a clean index: "
        f"incremental={sorted(_containers(store))} "
        f"clean={sorted(_containers(clean_store))}"
    )
    assert _containment(store) == _containment(clean_store), (
        "the nested directory's containment edges do not match a clean index"
    )
