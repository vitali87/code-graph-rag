# An incremental run re-parses the dependents of a CHANGED file (issue #1229
# phase 4) but never those of a DELETED one: the deleted file's subtree went
# with every edge into it, and nothing re-parsed the files that held those
# edges. A clean index of the remaining tree resolves their references the
# way it can (a phantom external parent, a module-anchored fallback, or no
# edge), so the two graphs disagreed until the next full update (issue #1567).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

JAVA: dict[str, str] = {
    "src/Base.java": "package app;\npublic class Base { public int run() { return 1; } }\n",
    "src/Derived.java": (
        "package app;\npublic class Derived extends Base {\n"
        "  @Override public int run() { return 2; }\n}\n"
    ),
}
PYTHON: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/base.py": "class Base:\n    def run(self):\n        return 1\n",
    "pkg/derived.py": (
        "from .base import Base\n\nclass Derived(Base):\n"
        "    def run(self):\n        return super().run() + 1\n"
    ),
}
CPP: dict[str, str] = {
    "shape.h": "namespace geo {\nclass Shape {\n public:\n  virtual int area();\n};\n}\n",
    "shape.cpp": (
        '#include "shape.h"\nnamespace geo {\nint Shape::area() { return 0; }\n}\n'
        "int use() { geo::Shape s; return s.area(); }\n"
    ),
}

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]
_STRUCTURE = {cs.NodeLabel.FILE.value, cs.NodeLabel.FOLDER.value}


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    # File and Folder nodes carry absolute paths, which differ between the
    # two temporary trees; everything else is keyed by project-relative qns.
    nodes = frozenset(
        (label, str(uid)) for (label, uid) in store.nodes if label not in _STRUCTURE
    )
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
        if fl not in _STRUCTURE and tl not in _STRUCTURE
    )
    return nodes, edges


def _materialise(root: Path, files: dict[str, str]) -> None:
    root.mkdir()
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


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


@pytest.mark.parametrize(
    ("language", "fixture", "deleted", "dependent_qn"),
    [
        (cs.SupportedLanguage.JAVA, JAVA, "src/Base.java", "proj.src.Derived.Derived"),
        (cs.SupportedLanguage.PYTHON, PYTHON, "pkg/base.py", "proj.pkg.derived"),
        (cs.SupportedLanguage.CPP, CPP, "shape.h", "proj.shape"),
    ],
    ids=["java-base-class", "python-imported-module", "cpp-header"],
)
def test_deleting_a_file_reparses_its_dependents(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    deleted: str,
    dependent_qn: str,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, fixture)
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    (root / deleted).unlink()
    _index(store, root, language, force=False)
    after = _snapshot(store)

    remaining = {rel: text for rel, text in fixture.items() if rel != deleted}
    clean_root = temp_repo / "clean" / "proj"
    clean_root.parent.mkdir()
    _materialise(clean_root, remaining)
    clean_store = _StatefulIngestor()
    _index(clean_store, clean_root, language, force=True)
    clean = _snapshot(clean_store)

    # The dependent's outgoing edges are the ones the deletion severed; a
    # clean index re-derives them from the remaining tree.
    def outgoing(snapshot: Snapshot) -> set[tuple[str, ...]]:
        return {e for e in snapshot[1] if e[1] == dependent_qn}

    assert outgoing(clean), "fixture must leave the dependent with an edge to compare"
    assert outgoing(after) == outgoing(clean)
    assert after == clean
