# Whether a directory is a Package or a Folder is decided every run by
# identify_structure, but the CONTAINS_MODULE edge of each module under it is
# emitted only when that module is parsed, and a Package node whose indicator
# file disappeared was never pruned. Adding or deleting pkg/__init__.py
# therefore left the sibling modules hanging off the old container and, on
# deletion, kept the Package node beside the new Folder (issue #1570).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/base.py": "class Base:\n    def run(self):\n        return 1\n",
    "pkg/derived.py": (
        "from .base import Base\n\nclass Derived(Base):\n"
        "    def run(self):\n        return super().run() + 1\n"
    ),
}
INIT = "pkg/__init__.py"
_ABSOLUTE = {cs.NodeLabel.FOLDER.value, cs.NodeLabel.PACKAGE.value}

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor, root: Path) -> Snapshot:
    # File nodes are keyed by absolute path and Folder nodes too; the two
    # trees live in different directories, so Folder ids are made relative
    # and File nodes are left out (their containment mirrors the Folder's).
    prefix = root.resolve().as_posix() + "/"

    def norm(label: str, uid: str) -> str:
        return (
            uid[len(prefix) :] if label in _ABSOLUTE and uid.startswith(prefix) else uid
        )

    nodes = frozenset(
        (label, norm(label, str(uid)))
        for (label, uid) in store.nodes
        if label != cs.NodeLabel.FILE.value
    )
    edges = frozenset(
        (str(fl), norm(str(fl), str(fv)), str(rel), str(tl), norm(str(tl), str(tv)))
        for (fl, fv, rel, tl, tv) in store.edges
        if cs.NodeLabel.FILE.value not in (fl, tl)
    )
    return nodes, edges


def _materialise(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.PYTHON not in parsers:
        pytest.skip("python parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _clean(temp_repo: Path, name: str, files: dict[str, str]) -> Snapshot:
    root = temp_repo / name / "proj"
    _materialise(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    return _snapshot(store, root)


def test_deleting_init_py_turns_the_package_into_a_folder(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    _materialise(root, FIXTURE)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    (root / INIT).unlink()
    _index(store, root, force=False)
    after = _snapshot(store, root)

    without_init = {rel: text for rel, text in FIXTURE.items() if rel != INIT}
    clean = _clean(temp_repo, "clean", without_init)
    assert (cs.NodeLabel.PACKAGE.value, "proj.pkg") not in after[0]
    assert after == clean


def test_adding_init_py_turns_the_folder_into_a_package(temp_repo: Path) -> None:
    root = temp_repo / "proj"
    without_init = {rel: text for rel, text in FIXTURE.items() if rel != INIT}
    _materialise(root, without_init)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    init = root / INIT
    init.write_text(FIXTURE[INIT], encoding="utf-8")
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(init, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, force=False)
    after = _snapshot(store, root)

    clean = _clean(temp_repo, "clean", FIXTURE)
    contains = {
        e[4]
        for e in after[1]
        if e[0] == cs.NodeLabel.PACKAGE.value
        and e[2] == cs.RelationshipType.CONTAINS_MODULE.value
    }
    assert contains == {"proj.pkg", "proj.pkg.base", "proj.pkg.derived"}
    assert after == clean
