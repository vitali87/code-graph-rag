"""Re-ingest hydration must not re-derive the whole repository (#1872).

`_hydrate_for_reingest` calls `identify_structure()` unscoped so a fresh
updater has the package map its parent lookups need. That call also EMITS a
`Package` or `Folder` node for every directory, as it is on disk now. Any
directory whose package-ness changed since the last index therefore gains a
node of the new kind beside its surviving old one -- two contradictory
container identities for one directory, for a directory the scoped call never
named.

`identify_structure`'s own docstring already describes this failure and names
`only` as the remedy, but hydration cannot use `only`: it needs the WHOLE map,
because a re-parsed module's parent lookup reads it for the enclosing package.
What it does not need is to emit anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater, ReingestAborted
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

UTIL = "def helper():\n    return 1\n"


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


def _kinds_by_directory(store: _StatefulIngestor) -> dict[str, set[str]]:
    """Container labels per DIRECTORY, keyed on absolute_path.

    Keyed on the path rather than the uid deliberately: a Package is keyed on
    a dotted qualified name and a Folder on an absolute path, so uid-to-uid
    comparison can never show that both describe one directory -- a duplicate
    reads as two unrelated entries. A probe written that way reported "no
    defect" against output that plainly showed one.
    """
    out: dict[str, set[str]] = {}
    for (label, _uid), props in store.nodes.items():
        if label not in (cs.NodeLabel.PACKAGE.value, cs.NodeLabel.FOLDER.value):
            continue
        path = str(props.get(cs.KEY_ABSOLUTE_PATH) or "")
        out.setdefault(path.rsplit("/", 1)[-1], set()).add(label)
    return out


def _two_packages(root: Path) -> None:
    for name in ("pkg", "other"):
        (root / name).mkdir(parents=True)
        (root / name / "__init__.py").write_text("", encoding="utf-8")
        (root / name / "m.py").write_text(UTIL, encoding="utf-8")


def test_hydration_does_not_give_an_unnamed_directory_two_identities(
    tmp_path: Path,
) -> None:
    """The issue: `other/` is never named, yet ends up both Package and Folder.

    A FRESH updater is the case that matters -- the MCP tool builds one per
    project, and a reused updater skips hydration entirely -- so a suite that
    only ever reuses one updater cannot see this.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _two_packages(root)

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()
    assert _kinds_by_directory(store)["other"] == {cs.NodeLabel.PACKAGE.value}, (
        "fixture guard: other/ must start as exactly a Package"
    )

    # Someone else's change, on disk. The re-ingest below never names it.
    (root / "other" / "__init__.py").unlink()
    (root / "pkg" / "__init__.py").unlink()

    _updater(root, store).reingest([], deleted=["pkg/__init__.py"])
    store.flush_all()

    duplicated = {
        directory: sorted(kinds)
        for directory, kinds in _kinds_by_directory(store).items()
        if len(kinds) > 1
    }
    assert not duplicated, (
        "the hydration re-derived the whole repo and gave a directory the "
        f"scoped call never named a second container identity: {duplicated}"
    )


def test_hydration_still_populates_the_package_map(tmp_path: Path) -> None:
    """The control: suppressing the EMISSION must not lose the MAP.

    Hydration derives the structure because a re-parsed module's parent
    lookup reads `structural_elements` for its enclosing package; without it
    the module hangs off a Folder instead. A fix that simply stopped deriving
    would satisfy the test above and break that, so this pins the reason the
    call is there at all.
    """
    root = tmp_path / "incremental"
    root.mkdir()
    _two_packages(root)

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()

    fresh = _updater(root, store)
    fresh._hydrate_for_reingest()

    elements = fresh.factory.structure_processor.structural_elements
    assert elements.get(Path("pkg")) == "proj.pkg", (
        "hydration did not record pkg/ as a package, so a re-parsed module "
        f"there would hang off a Folder: {dict(elements)}"
    )
    assert elements.get(Path("other")) == "proj.other", (
        "hydration did not record other/ as a package"
    )


def test_suppressing_emission_writes_nothing_and_records_both_kinds(
    tmp_path: Path,
) -> None:
    """`emit=False` is map-only, and the map covers folders as well.

    Asserted directly rather than through a re-ingest because this is the
    property the whole fix rests on: if it recorded only packages, a plain
    directory's parent lookup would miss and the re-parsed module would hang
    off the wrong container -- a failure that would surface far from here.

    A folder is recorded as `None`, which is what the map means by "this
    directory is not a package"; absent and None are different answers to a
    parent lookup.
    """
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "plain").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(UTIL, encoding="utf-8")
    (root / "plain" / "n.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    updater = _updater(root, store)
    before = len(store.nodes)

    updater.factory.structure_processor.identify_structure(emit=False)

    elements = updater.factory.structure_processor.structural_elements
    assert elements.get(Path("pkg")) == "proj.pkg", (
        "a package was not recorded, so a re-parsed module under it would "
        "hang off a Folder"
    )
    assert Path("plain") in elements and elements[Path("plain")] is None, (
        "a plain directory was not recorded at all; absent and None are "
        f"different answers to a parent lookup: {dict(elements)}"
    )
    assert len(store.nodes) == before, (
        "emit=False wrote nodes, which is the whole thing it exists not to do"
    )


def _containment_from_missing_containers(store: _StatefulIngestor) -> list[tuple]:
    """CONTAINS_* edges whose SOURCE container node does not exist.

    In production this is worse than a stray edge: the merge query MATCHes
    both endpoints, so a missing source means the edge is never created at
    all and the new file and module are silently unparented.
    """
    uids = {(label, str(uid)) for (label, uid) in store.nodes}
    return [
        (sl, str(s_), r, tl, str(t))
        for (sl, s_, r, tl, t) in store.edges
        if "CONTAINS" in r and (sl, str(s_)) not in uids
    ]


def test_a_file_added_in_a_NEW_directory_still_gets_its_container(
    tmp_path: Path,
) -> None:
    """Suppressing hydration's emission must not orphan a new directory.

    Hydration's unscoped walk was the only writer of the container node for a
    directory that is NEW since the last index. The scoped re-derivation does
    not cover it: it re-derives directories whose package-ness FLIPPED, and a
    brand-new directory never flipped -- it simply was not there before.

    So `emit=False` alone left `CONTAINS_FILE` and `CONTAINS_MODULE` edges
    pointing at a container node nothing had written (greptile-local, #1872).
    """
    root = tmp_path / "incremental"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()

    # Both kinds, because they are written by different branches: a plain
    # directory becomes a Folder and one with an indicator becomes a Package.
    (root / "newplain").mkdir()
    (root / "newplain" / "n.py").write_text(UTIL, encoding="utf-8")
    (root / "newpkg").mkdir()
    (root / "newpkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "newpkg" / "p.py").write_text(UTIL, encoding="utf-8")

    _updater(root, store).reingest(
        ["newplain/n.py", "newpkg/__init__.py", "newpkg/p.py"]
    )
    store.flush_all()

    kinds = _kinds_by_directory(store)
    assert kinds.get("newplain") == {cs.NodeLabel.FOLDER.value}, (
        f"a new plain directory got no Folder node: {kinds}"
    )
    assert kinds.get("newpkg") == {cs.NodeLabel.PACKAGE.value}, (
        f"a new package directory got no Package node: {kinds}"
    )
    dangling = _containment_from_missing_containers(store)
    assert not dangling, (
        "containment edges point at container nodes nothing wrote, so in "
        f"production the new files would be unparented: {dangling}"
    )


def test_a_file_added_in_a_new_SUBdirectory_still_gets_its_container(
    tmp_path: Path,
) -> None:
    """The nested case, which the flat one above does not cover.

    A new directory under an EXISTING package has a parent the graph already
    holds, so a fix that only handled top-level additions would pass the test
    above and still orphan this one.
    """
    root = tmp_path / "incremental"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()

    (root / "pkg" / "sub").mkdir()
    (root / "pkg" / "sub" / "q.py").write_text(UTIL, encoding="utf-8")

    _updater(root, store).reingest(["pkg/sub/q.py"])
    store.flush_all()

    assert "sub" in _kinds_by_directory(store), (
        f"a new subdirectory got no container node at all: {_kinds_by_directory(store)}"
    )
    dangling = _containment_from_missing_containers(store)
    assert not dangling, (
        f"the new subdirectory's containment edges are unparented: {dangling}"
    )


def test_an_unreadable_container_query_aborts_before_deriving(
    tmp_path: Path,
) -> None:
    """ "I could not ask" must not be read as "there is nothing there".

    `_recorded_container_kinds` returns an empty set for BOTH cases. If the
    uncontained walk treated a failed read as an absent container, a
    transient store failure would derive and emit EVERY ancestor, giving any
    that had changed on disk a second identity beside its old one -- the
    duplicate-container defect this whole PR exists to remove, recreated by
    the fix for it (Greptile, PR #1875).

    Aborting is safe here because it happens in the read-only prologue,
    before any delete or content write, which is exactly what
    `ReingestAborted` means.
    """
    from unittest.mock import patch

    root = tmp_path / "incremental"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()
    before = dict(store.nodes)

    (root / "newdir").mkdir()
    (root / "newdir" / "n.py").write_text(UTIL, encoding="utf-8")

    updater = _updater(root, store)
    real_fetch = store.fetch_all

    def flaky(query: str, params: dict | None = None):
        if query == cs.CYPHER_CONTAINER_KIND:
            raise RuntimeError("store went away")
        return real_fetch(query, params)

    with patch.object(store, "fetch_all", side_effect=flaky):
        with pytest.raises(ReingestAborted):
            updater.reingest(["newdir/n.py"])

    assert store.nodes == before, (
        "the aborted re-ingest wrote nodes, so it was not the read-only "
        "prologue it claims to abort in"
    )


def test_a_sink_without_a_query_surface_still_reingests(tmp_path: Path) -> None:
    """The control: no query surface is a configuration, not a failure.

    An earlier version of the abort above could not tell "this sink cannot
    answer queries at all" from "the query raised", and aborted every
    re-ingest against a plain sink -- reddening five of main's own tests.
    """
    from unittest.mock import patch

    root = tmp_path / "incremental"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "m.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()

    (root / "newdir").mkdir()
    (root / "newdir" / "n.py").write_text(UTIL, encoding="utf-8")

    updater = _updater(root, store)
    # `isinstance(..., QueryProtocol)` is what the guard consults, so failing
    # that check is how a surface-less sink presents.
    with patch("codebase_rag.graph_updater.QueryProtocol", type(None)):
        report = updater.reingest(["newdir/n.py"])

    assert report.reparsed, "a sink without a query surface could not re-ingest at all"


def test_a_read_that_fails_on_the_SECOND_ask_still_aborts(tmp_path: Path) -> None:
    """One guarded read, not two.

    An earlier version asked twice: once to test readability, once for the
    value. A failure BETWEEN the two turned an existing container into "no
    container", so the directory was treated as new -- the disk-derived
    identity was emitted WITHOUT joining `flipped_dirs`, the stale node was
    never pruned, and both identities remained. The guard reopened the very
    defect it was added to close (Greptile, PR #1875).

    Driven by failing only the SECOND container query, which is exactly the
    window the two-read version left open and the one-read version does not
    have.
    """
    from unittest.mock import patch

    root = tmp_path / "incremental"
    (root / "outer").mkdir(parents=True)
    (root / "outer" / "__init__.py").write_text("", encoding="utf-8")
    (root / "outer" / "m.py").write_text(UTIL, encoding="utf-8")

    store = _StatefulIngestor()
    _updater(root, store).run(force=True)
    store.flush_all()

    (root / "outer" / "new").mkdir()
    (root / "outer" / "new" / "mod.py").write_text(UTIL, encoding="utf-8")

    updater = _updater(root, store)
    real_fetch = store.fetch_all
    asks = {"n": 0}

    def flaky(query: str, params: dict | None = None):
        if query == cs.CYPHER_CONTAINER_KIND:
            asks["n"] += 1
            if asks["n"] >= 2:
                raise RuntimeError("store went away mid-walk")
        return real_fetch(query, params)

    with patch.object(store, "fetch_all", side_effect=flaky):
        with pytest.raises(ReingestAborted):
            updater.reingest(["outer/new/mod.py"])

    assert asks["n"] >= 2, (
        "fixture guard: the container query must have been asked at least "
        "twice, or this test does not reach the window it is about"
    )
    duplicated = {
        directory: sorted(kinds)
        for directory, kinds in _kinds_by_directory(store).items()
        if len(kinds) > 1
    }
    assert not duplicated, (
        f"a mid-walk read failure left a directory with two identities: {duplicated}"
    )
