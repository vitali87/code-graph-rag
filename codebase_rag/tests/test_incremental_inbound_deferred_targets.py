# An incremental run re-parses every file holding a dependency edge into a
# re-indexed file (issue #1229 phase 4) and restores the remaining inbound
# edges verbatim (issue #532). The eval's in-memory graph stand-in used to
# answer the dependents query with nothing, so under it no file was ever
# re-parsed as a dependent and every cross-file edge relied on the restore,
# which cannot carry an edge whose target is registered only after Pass 2
# (a Go receiver method) or whose node the delete cascade took from another
# file (a C++ method declared in a header and defined out of class). A clean
# index and a production run keep both edges (issue #1560).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

GO_FIXTURE: dict[str, str] = {
    "go.mod": "module example.com/app\n\ngo 1.21\n",
    "base.go": "package app\n\ntype Base struct{ X int }\n\nfunc (b Base) Run() int { return b.X }\n",
    "derived.go": (
        "package app\n\ntype Derived struct { Base }\n\n"
        "func Make() int { d := Derived{}; return d.Run() }\n"
    ),
}
GO_CALL = ("proj.derived.Make", "proj.base.Base.Run")

CPP_FIXTURE: dict[str, str] = {
    "shape.h": "namespace geo {\nclass Shape {\n public:\n  virtual int area();\n};\n}\n",
    "shape.cpp": (
        '#include "shape.h"\nnamespace geo {\nint Shape::area() { return 0; }\n}\n'
        "int use() { geo::Shape s; return s.area(); }\n"
    ),
}
CPP_CALL = ("proj.shape.use", "proj.shape.h.geo.Shape.area")

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
    )
    return nodes, edges


def _calls(snapshot: Snapshot) -> set[tuple[str, str]]:
    return {
        (e[1], e[4]) for e in snapshot[1] if e[2] == cs.RelationshipType.CALLS.value
    }


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


def _touch_after_cache(path: Path, root: Path) -> None:
    # The incremental pass skips a cached file whose mtime is not newer than
    # the hash cache's, without hashing it; on a coarse-timestamp filesystem
    # a write landing in the cache's own tick would be skipped and the test
    # would pass without re-parsing anything. Place the edit past the cache.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(path.read_text(encoding="utf-8") + "// touched\n")
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))


@pytest.mark.parametrize(
    ("language", "fixture", "edited", "call"),
    [
        (cs.SupportedLanguage.GO, GO_FIXTURE, "base.go", GO_CALL),
        (cs.SupportedLanguage.CPP, CPP_FIXTURE, "shape.h", CPP_CALL),
    ],
    ids=["go-receiver-method", "cpp-header-declared-method"],
)
def test_incremental_reindex_keeps_inbound_calls_to_deferred_targets(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    edited: str,
    call: tuple[str, str],
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in fixture.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    clean = _snapshot(store)
    assert call in _calls(clean), "fixture must produce the cross-file call"

    # A trailing comment changes the hash but not the AST: the defining file
    # re-parses and the caller joins it as a dependent, so its edge into the
    # re-indexed file is recomputed rather than restored.
    _touch_after_cache(root / edited, root)
    _index(store, root, language, force=False)
    after = _snapshot(store)

    assert call in _calls(after)
    assert after == clean
