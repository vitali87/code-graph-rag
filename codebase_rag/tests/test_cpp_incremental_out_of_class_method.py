# On the batch incremental path, deferred out-of-class C++ methods used to be
# resolved BEFORE the registry was rehydrated from the graph. A re-parsed
# `.cpp` whose class lives in an unchanged header therefore could not find the
# class, fell back to a module-anchored qn (`proj.derived.Derived.run` beside
# the clean index's `proj.derived.h.Derived.run`) and was anchored to its
# Module with DEFINES: a phantom second node whose presence depended on parse
# order (issue #1552).
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FIXTURE: dict[str, str] = {
    "base.h": "class Base {\n public:\n  virtual int run();\n};\n",
    "derived.h": (
        '#include "base.h"\n\nclass Derived : public Base {\n public:\n'
        "  int run() override;\n};\n"
    ),
    "derived.cpp": '#include "derived.h"\n\nint Derived::run() { return 1; }\n',
}

Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        for (fl, fv, rel, tl, tv) in store.edges
    )
    return nodes, edges


def _index(store: _StatefulIngestor, repo: Path, force: bool) -> None:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    ).run(force=force)


@pytest.mark.parametrize("edited", ["derived.cpp", "derived.h", "base.h"])
def test_incremental_reparse_registers_out_of_class_method_once(
    temp_repo: Path, edited: str
) -> None:
    root = temp_repo / "proj"
    root.mkdir()
    for rel, text in FIXTURE.items():
        (root / rel).write_text(text, encoding="utf-8")
    store = _StatefulIngestor()
    _index(store, root, force=True)
    clean = _snapshot(store)
    class_qn = "proj.derived.h.Derived"
    method_qn = f"{class_qn}.run"
    assert (cs.NodeLabel.METHOD.value, method_qn) in clean[0]

    # A trailing comment changes the hash but not the AST, so only the edited
    # file re-parses; the others are rehydration-only.
    path = root / edited
    path.write_text(path.read_text(encoding="utf-8") + "// touched\n")
    _index(store, root, force=False)
    nodes, edges = _snapshot(store)

    methods = {
        qn
        for (label, qn) in nodes
        if label == cs.NodeLabel.METHOD.value and qn.endswith(".Derived.run")
    }
    assert methods == {method_qn}
    assert (
        cs.NodeLabel.CLASS.value,
        class_qn,
        cs.RelationshipType.DEFINES_METHOD.value,
        cs.NodeLabel.METHOD.value,
        method_qn,
    ) in edges
    module_defines_method = {
        e
        for e in edges
        if e[0] == cs.NodeLabel.MODULE.value
        and e[2] == cs.RelationshipType.DEFINES.value
        and e[3] == cs.NodeLabel.METHOD.value
    }
    assert module_defines_method == set()
    assert (nodes, edges) == clean
