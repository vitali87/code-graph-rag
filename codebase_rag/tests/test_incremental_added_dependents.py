# An incremental run re-parses the dependents of a CHANGED file through the
# edges that point into it, so a file ADDED by the run has no dependents at
# all: the unchanged files that referenced its definitions while it was
# missing keep the resolution they had then (a phantom external parent, a
# dropped import, no CALLS edge), and the graph differs from a clean index of
# the same tree until those files happen to change (issue #1568, the
# addition half of #1567).
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.test_incremental_deleted_dependents import (
    CPP,
    JAVA,
    PYTHON,
    Snapshot,
    _index,
    _materialise,
    _snapshot,
)
from evals.cgr_graph import _StatefulIngestor

GO: dict[str, str] = {
    "go.mod": "module example.com/app\n\ngo 1.22\n",
    "base.go": "package app\n\ntype Base struct{}\n\nfunc (b Base) Run() int { return 1 }\n",
    "derived.go": "package app\n\nfunc Make() int {\n\tb := Base{}\n\treturn b.Run()\n}\n",
}
TYPESCRIPT: dict[str, str] = {
    "base.ts": "export class Base {\n  run(): number { return 1 }\n}\n",
    "derived.ts": (
        "import { Base } from './base'\n\nexport class Derived extends Base {\n"
        "  run(): number { return 2 }\n}\n"
    ),
}


def _add_after_cache(root: Path, rel: str, text: str) -> None:
    # The incremental pass trusts directory mtimes that are not newer than
    # the hash cache's; place the addition past the cache.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    for touched in (path, path.parent):
        os.utime(touched, (cache_mtime + 1, cache_mtime + 1))


@pytest.mark.parametrize(
    ("language", "fixture", "added", "dependent_qn"),
    [
        (cs.SupportedLanguage.JAVA, JAVA, "src/Base.java", "proj.src.Derived.Derived"),
        (cs.SupportedLanguage.PYTHON, PYTHON, "pkg/base.py", "proj.pkg.derived"),
        (cs.SupportedLanguage.CPP, CPP, "shape.h", "proj.shape"),
        (cs.SupportedLanguage.GO, GO, "base.go", "proj.derived.Make"),
        (cs.SupportedLanguage.TS, TYPESCRIPT, "base.ts", "proj.derived.Derived"),
    ],
    ids=[
        "java-base-class",
        "python-imported-module",
        "cpp-header",
        "go-call",
        "ts-base",
    ],
)
def test_adding_a_file_reparses_the_files_that_waited_for_it(
    temp_repo: Path,
    language: cs.SupportedLanguage,
    fixture: dict[str, str],
    added: str,
    dependent_qn: str,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, {rel: text for rel, text in fixture.items() if rel != added})
    store = _StatefulIngestor()
    _index(store, root, language, force=True)
    _add_after_cache(root, added, fixture[added])
    _index(store, root, language, force=False)
    after = _snapshot(store)

    clean_root = temp_repo / "clean" / "proj"
    clean_root.parent.mkdir()
    _materialise(clean_root, fixture)
    clean_store = _StatefulIngestor()
    _index(clean_store, clean_root, language, force=True)
    clean = _snapshot(clean_store)

    # The dependent's outgoing edges are the ones the missing file left
    # unresolved; a clean index of the full tree resolves them.
    def outgoing(snapshot: Snapshot) -> set[tuple[str, ...]]:
        return {e for e in snapshot[1] if e[1] == dependent_qn}

    assert outgoing(clean), "fixture must give the dependent an edge to compare"
    assert outgoing(after) == outgoing(clean)
    assert after == clean


def test_a_module_records_what_it_could_not_resolve_and_clears_it(
    temp_repo: Path,
) -> None:
    """The fact an added file is matched against: a module's unresolved
    references (a dropped import, a base that resolved nowhere, a call with
    no callee) live on its node, and a re-parse that resolves them writes
    the list without them, since a merged property is never removed
    otherwise."""
    root = temp_repo / "proj"
    _materialise(
        root, {rel: text for rel, text in PYTHON.items() if rel != "pkg/base.py"}
    )
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    module = store.nodes[(cs.NodeLabel.MODULE.value, "proj.pkg.derived")]
    recorded = module[cs.KEY_UNRESOLVED_REFERENCES]
    # The base class resolved nowhere; the import itself became a phantom
    # ExternalModule, which the existing importer lookup finds from the
    # graph, so it is not recorded twice. The unresolved calls (`super()`,
    # `.run()`) are not recorded either: in Python a callee from another
    # file needs an import, so recording them would only re-parse every
    # module calling a method of that name when such a file is added.
    assert recorded == ["Base"]

    _add_after_cache(root, "pkg/base.py", PYTHON["pkg/base.py"])
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    module = store.nodes[(cs.NodeLabel.MODULE.value, "proj.pkg.derived")]
    assert module[cs.KEY_UNRESOLVED_REFERENCES] == []


def test_a_go_module_records_its_unresolved_calls(temp_repo: Path) -> None:
    """Go sees a same-package definition with no import, so the callee's
    name is the only link to the file that will define it."""
    root = temp_repo / "proj"
    _materialise(root, {rel: text for rel, text in GO.items() if rel != "base.go"})
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.GO, force=True)
    module = store.nodes[(cs.NodeLabel.MODULE.value, "proj.derived")]
    assert "Run" in module[cs.KEY_UNRESOLVED_REFERENCES]


def test_the_batch_path_asks_both_added_file_lookups(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The importer lookup existed for the scoped path only (issue #1682);
    the batch path never called it, which is half of this issue. Both
    lookups are asked with the added keys, and only with them."""
    root = temp_repo / "proj"
    _materialise(
        root, {rel: text for rel, text in PYTHON.items() if rel != "pkg/base.py"}
    )
    store = _StatefulIngestor()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    _add_after_cache(root, "pkg/base.py", PYTHON["pkg/base.py"])
    asked: dict[str, list[str]] = {}
    importers = GraphUpdater._unresolved_importer_keys
    waiters = GraphUpdater._unresolved_reference_waiters

    def spy_importers(self: GraphUpdater, keys: list[str]) -> list[str]:
        asked["importers"] = list(keys)
        return importers(self, keys)

    def spy_waiters(self: GraphUpdater, added: list[tuple[str, bytes]]) -> list[str]:
        asked["waiters"] = [key for key, _b in added]
        return waiters(self, added)

    monkeypatch.setattr(GraphUpdater, "_unresolved_importer_keys", spy_importers)
    monkeypatch.setattr(GraphUpdater, "_unresolved_reference_waiters", spy_waiters)
    _index(store, root, cs.SupportedLanguage.PYTHON, force=False)
    assert asked == {"importers": ["pkg/base.py"], "waiters": ["pkg/base.py"]}
    # A full build has no waiter in the graph: nothing is offered, so the
    # added files are not parsed a second time for their names.
    asked.clear()
    _index(store, root, cs.SupportedLanguage.PYTHON, force=True)
    assert asked == {"importers": [], "waiters": []}


def test_the_scoped_path_offers_created_files_only(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A modified file's definitions were already there to resolve against;
    only a file the call created can satisfy a recorded name, so an edit
    that defines a common method name does not re-parse every module
    calling one (local review)."""
    from codebase_rag.parser_loader import load_parsers

    root = temp_repo / "proj"
    _materialise(
        root, {rel: text for rel, text in PYTHON.items() if rel != "pkg/base.py"}
    )
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    asked: list[list[str]] = []
    waiters = GraphUpdater._unresolved_reference_waiters

    def spy(self: GraphUpdater, added: list[tuple[str, bytes]]) -> list[str]:
        asked.append([key for key, _b in added])
        return waiters(self, added)

    monkeypatch.setattr(GraphUpdater, "_unresolved_reference_waiters", spy)
    (root / "pkg/derived.py").write_text(
        PYTHON["pkg/derived.py"] + "\n\ndef get():\n    return 1\n"
    )
    updater.reingest(["pkg/derived.py"])
    (root / "pkg/base.py").write_text(PYTHON["pkg/base.py"])
    updater.reingest(["pkg/base.py"])
    assert asked == [[], ["pkg/base.py"]]
