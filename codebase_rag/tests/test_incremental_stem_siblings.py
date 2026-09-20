# Two files sharing a stem (shape.h / shape.cpp) strip to one module qn; the
# disambiguator gives the bare qn to the first one processed and suffixes the
# other, and an incremental run seeds the taken qns from the graph so an
# added file cannot overwrite a sibling's module (issue #897). The seed also
# froze the CLAIMANT: after the bare-qn holder was deleted, or a sibling that
# would win the bare qn was added, the survivor kept the qn it was first
# given, so every qualified name in the pair depended on index history rather
# than on the tree (issue #1569).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

CPP: dict[str, str] = {
    "shape.h": "namespace geo {\nclass Shape {\n public:\n  virtual int area();\n};\n}\n",
    "shape.cpp": (
        '#include "shape.h"\nnamespace geo {\nint Shape::area() { return 0; }\n}\n'
        "int use() { geo::Shape s; return s.area(); }\n"
    ),
    # A dependent whose only edges reach the survivor: `all.h` includes the
    # header, and `main.cpp` reaches the pair through it. A direct includer
    # that calls `area()` would already be found through the DELETED file
    # (the out-of-line definition owns the method's path), so it pins the
    # add direction only; this shape pins both.
    "all.h": '#include "shape.h"\n',
    "main.cpp": '#include "all.h"\nint main() { geo::Shape s; return s.area(); }\n',
}
C: dict[str, str] = {
    "util.h": (
        "int add(int a, int b);\nstatic inline int twice(int a) { return a * 2; }\n"
    ),
    "util.c": '#include "util.h"\nint add(int a, int b) { return a + b; }\n',
    # A `.c` includer carries both an IMPORTS edge to the header (since
    # #1654) and a CALLS edge into its inline function; both must follow the
    # survivor's changing qn.
    "main.c": '#include "util.h"\nint main(void) { return twice(1); }\n',
}
_ABSOLUTE = {cs.NodeLabel.FOLDER.value, cs.NodeLabel.PACKAGE.value}

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor, root: Path) -> Snapshot:
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
        (root / rel).write_text(text, encoding="utf-8")


def _index(
    store: _StatefulIngestor, repo: Path, language: cs.SupportedLanguage, force: bool
) -> None:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


def _clean(
    temp_repo: Path, name: str, files: dict[str, str], language: cs.SupportedLanguage
) -> Snapshot:
    root = temp_repo / name / "proj"
    _materialise(root, files)
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    return _snapshot(store, root)


def _modules(snapshot: Snapshot) -> set[str]:
    return {qn for (label, qn) in snapshot[0] if label == cs.NodeLabel.MODULE.value}


CASES = [
    (cs.SupportedLanguage.CPP, CPP, "shape.cpp", "proj.shape"),
    (cs.SupportedLanguage.C, C, "util.c", "proj.util"),
]


@pytest.mark.parametrize(("language", "fixture", "source", "bare_qn"), CASES)
def test_deleting_the_bare_qn_holder_hands_the_qn_to_the_surviving_sibling(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    source: str,
    bare_qn: str,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, fixture)
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    assert bare_qn in _modules(_snapshot(store, root)), "the source owns the bare qn"
    (root / source).unlink()
    _index(store, root, language, force=False)
    after = _snapshot(store, root)

    header_only = {rel: text for rel, text in fixture.items() if rel != source}
    clean = _clean(temp_repo, "clean", header_only, language)
    assert bare_qn in _modules(after)
    assert _modules(after) == _modules(clean)
    assert after == clean


@pytest.mark.parametrize(("language", "fixture", "source", "bare_qn"), CASES)
def test_adding_a_sibling_that_wins_the_bare_qn_renames_the_existing_one(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    source: str,
    bare_qn: str,
) -> None:
    root = temp_repo / "proj"
    header_only = {rel: text for rel, text in fixture.items() if rel != source}
    _materialise(root, header_only)
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    assert bare_qn in _modules(_snapshot(store, root)), "alone, the header owns it"
    added = root / source
    added.write_text(fixture[source], encoding="utf-8")
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(added, (cache_mtime + 1, cache_mtime + 1))
    _index(store, root, language, force=False)
    after = _snapshot(store, root)

    clean = _clean(temp_repo, "clean", fixture, language)
    assert _modules(after) == _modules(clean)
    assert after == clean
