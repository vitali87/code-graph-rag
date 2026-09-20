# A GraphUpdater can be reused across runs (the watcher keeps one and
# removes deleted files from its state; a caller may hold one across `run()`
# calls). A deleted file's in-memory state (registry entries, simple-name
# lookup, module-qn map) used to be removed only in the late deletion block,
# after Pass 2 had parsed the changed files, so a rename or move done in one
# cycle resolved the re-parsed importer against the deleted definitions and a
# same-stem replacement got a suffixed qn. A fresh updater never saw this
# (issue #1575).
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag import graph_updater as gu
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.frontends import go as go_fe
from codebase_rag.parsers.frontends import java as java_fe
from codebase_rag.parsers.go_frontend import GoCallSite, GoSemanticFacts
from codebase_rag.parsers.java_frontend import JavaCallSite, JavaSemanticFacts
from evals.cgr_graph import _StatefulIngestor

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
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, repo: Path, language: cs.SupportedLanguage
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if language not in parsers:
        pytest.skip(f"{language} parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )


def _bump(root: Path, rel: str) -> None:
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def _clean(
    temp_repo: Path, files: dict[str, str], language: cs.SupportedLanguage
) -> Snapshot:
    root = temp_repo / "clean" / "proj"
    _materialise(root, files)
    store = _StatefulIngestor()
    _updater(store, root, language).run(force=True)
    return _snapshot(store, root)


PY_BEFORE = {
    "base.py": "class Base:\n    pass\n",
    "main.py": "from base import Base\n\ndef go():\n    return Base()\n",
}
PY_AFTER = {
    "core.py": "class Base:\n    pass\n",
    "main.py": "from core import Base\n\ndef go():\n    return Base()\n",
}
JS_BEFORE = {
    "util.js": "export function helper() { return 1; }\n",
    "main.js": "import { helper } from './util';\nexport function go() { return helper(); }\n",
}
JS_AFTER = {
    "util.ts": "export function helper(): number { return 1; }\n",
    "main.js": JS_BEFORE["main.js"],
}


def test_a_reused_updater_resolves_a_rename_against_the_new_module(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, PY_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    (root / "base.py").rename(root / "core.py")
    (root / "main.py").write_text(PY_AFTER["main.py"], encoding="utf-8")
    _bump(root, "core.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    instantiates = {
        e[4] for e in after[1] if e[2] == cs.RelationshipType.INSTANTIATES.value
    }
    assert instantiates == {"proj.core.Base"}
    assert after == _clean(temp_repo, PY_AFTER, cs.SupportedLanguage.PYTHON)


def test_a_reused_updater_gives_a_same_stem_replacement_the_bare_qn(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, JS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    (root / "util.js").unlink()
    (root / "util.ts").write_text(JS_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    modules = {qn for (label, qn) in after[0] if label == cs.NodeLabel.MODULE.value}
    assert modules == {"proj.util", "proj.main"}
    assert after == _clean(temp_repo, JS_AFTER, cs.SupportedLanguage.JS)


# `main.py` names `helper` without an import so the resolver's simple-name
# fallback, which is where the module-language check lives, decides the edge.
RS_PY_BEFORE = {
    "util.rs": "pub fn helper() -> i32 { 1 }\n",
    "main.py": "def go():\n    return helper()\n",
}
RS_PY_AFTER = {
    "util.py": "def helper():\n    return 1\n",
    "main.py": RS_PY_BEFORE["main.py"] + "\n",
}


def test_a_reused_updater_forgets_a_deleted_module_language(
    temp_repo: Path,
) -> None:
    # `reset_resolution_caches` must also drop the qn -> language memo:
    # after `util.rs` is replaced by `util.py`, the bare qn `proj.util` names
    # a Python module, and a stale RUST answer makes the resolver refuse the
    # Python -> Rust call a clean index resolves.
    root = temp_repo / "proj"
    _materialise(root, RS_PY_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    if cs.SupportedLanguage.RUST not in updater.parsers:
        pytest.skip("rust parser not available")
    updater.run(force=True)
    (root / "util.rs").unlink()
    (root / "util.py").write_text(RS_PY_AFTER["util.py"], encoding="utf-8")
    (root / "main.py").write_text(RS_PY_AFTER["main.py"], encoding="utf-8")
    _bump(root, "util.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    calls = {(e[1], e[4]) for e in after[1] if e[2] == cs.RelationshipType.CALLS.value}
    assert ("proj.main.go", "proj.util.helper") in calls
    assert after == _clean(temp_repo, RS_PY_AFTER, cs.SupportedLanguage.PYTHON)


def test_a_rehydrated_updater_forgets_a_deleted_definitions_language(
    temp_repo: Path,
) -> None:
    # The watcher's shape: a fresh updater whose FIRST run is incremental,
    # so the untouched util.rs definitions are read back from the graph and
    # `rehydrated_definition_paths` records them. `_module_language` falls
    # through to those paths for a definition qn, so removing the file's
    # state must drop them too, or the replacement util.py still answers
    # RUST for `proj.util.helper` and the call it now provides is refused.
    root = temp_repo / "proj"
    before = {**RS_PY_BEFORE, "other.py": "def x():\n    return 1\n"}
    after = {**RS_PY_AFTER, "other.py": "def x():\n    return 2\n"}
    _materialise(root, before)
    store = _StatefulIngestor()
    first = _updater(store, root, cs.SupportedLanguage.PYTHON)
    if cs.SupportedLanguage.RUST not in first.parsers:
        pytest.skip("rust parser not available")
    first.run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text(after["other.py"], encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    rehydrated = updater.factory.definition_processor.rehydrated_definition_paths
    assert "proj.util.helper" in rehydrated, "the shape did not rehydrate"

    (root / "util.rs").unlink()
    (root / "util.py").write_text(after["util.py"], encoding="utf-8")
    (root / "main.py").write_text(after["main.py"], encoding="utf-8")
    _bump(root, "util.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after_snapshot = _snapshot(store, root)

    calls = {
        (e[1], e[4])
        for e in after_snapshot[1]
        if e[2] == cs.RelationshipType.CALLS.value
    }
    assert ("proj.main.go", "proj.util.helper") in calls
    assert after_snapshot == _clean(temp_repo, after, cs.SupportedLanguage.PYTHON)


def test_a_rehydrated_updater_forgets_a_deleted_module_qn(temp_repo: Path) -> None:
    # Module qns read back from the graph on an incremental first run feed
    # `known_module_paths`, which deferred resolution treats as the set of
    # live internal modules. Deleting the file must drop its qn from that
    # set as well, or the reused updater keeps resolving against a module
    # that no longer exists.
    root = temp_repo / "proj"
    before = {
        "util.py": "def helper():\n    return 1\n",
        "main.py": "from util import helper\n\n\ndef go():\n    return helper()\n",
        "other.py": "def x():\n    return 1\n",
    }
    _materialise(root, before)
    store = _StatefulIngestor()
    _updater(store, root, cs.SupportedLanguage.PYTHON).run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    assert "proj.util" in updater._rehydrated_module_qns, "the shape did not rehydrate"
    assert "proj.util" in updater.known_module_paths()

    (root / "util.py").unlink()
    (root / "main.py").write_text("def go():\n    return 1\n", encoding="utf-8")
    _bump(root, "main.py")
    updater.run(force=False)

    assert "proj.util" not in updater.known_module_paths()


def test_a_deleted_mod_rs_is_swept_on_its_recorded_qn(temp_repo: Path) -> None:
    # `mod.rs` names its directory: src/a/mod.rs records as proj.src.a, while
    # the path-derived prefix is proj.src.a.mod. Keying the registry sweep on
    # the path-derived form swept a qn nothing recorded, so a reused updater
    # (watcher, MCP server) kept the deleted file's definitions and Pass 3
    # could still resolve calls into them.
    #
    # The recorded qn is seeded directly rather than parsed: the collapse is a
    # property of remove_file_from_state, and driving it through a real parse
    # would make the test skip wherever the Rust grammar is unavailable.
    root = temp_repo / "proj"
    (root / "src" / "a").mkdir(parents=True, exist_ok=True)
    mod_rs = root / "src" / "a" / "mod.rs"
    mod_rs.write_text("pub fn helper() -> i32 { 42 }\n", encoding="utf-8")

    updater = _updater(_StatefulIngestor(), root, cs.SupportedLanguage.PYTHON)
    updater.factory.definition_processor.module_qn_to_file_path["proj.src.a"] = mod_rs
    updater.function_registry.insert("proj.src.a.helper", "Function")
    updater.simple_name_lookup.setdefault("helper", set()).add("proj.src.a.helper")

    updater.remove_file_from_state(mod_rs)

    assert not [qn for qn in updater.function_registry.keys() if "helper" in qn], (
        "the deleted mod.rs left registry entries behind"
    )
    for qn_set in updater.simple_name_lookup.values():
        assert not [qn for qn in qn_set if "helper" in qn]


def test_rehydration_rebuilds_module_qns_instead_of_accumulating(
    temp_repo: Path,
) -> None:
    # The qn sets rehydration fills are rebuilt from the graph on every pass,
    # not accumulated across passes. `remove_file_from_state` only drops the
    # qns this updater removed itself, so a Module deleted by another writer
    # -- a second updater on the same project, `delete_project` then a
    # re-index, a run in another clone -- would otherwise linger and keep
    # being offered to deferred import verification as a live target.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    _updater(store, root, cs.SupportedLanguage.PYTHON).run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    assert "proj.util" in updater._rehydrated_module_qns, "the shape did not rehydrate"

    # Another writer deletes the Module from the graph. This updater is never
    # told: it does not re-parse the file and no removal runs through it, so
    # rehydration is the only thing that can drop the qn.
    removed = [key for key in store.nodes if key[1] == "proj.util"]
    assert removed, "expected a proj.util node to delete"
    for key in removed:
        del store.nodes[key]

    updater._rehydrate_registry_from_graph()

    assert "proj.util" not in updater._rehydrated_module_qns


def test_a_failed_module_query_leaves_the_rehydrated_set_intact(
    temp_repo: Path,
) -> None:
    # The clear sits after the fetch on purpose: a full build degrades on a
    # failed module query and returns early, and clearing before it would
    # drop the set rather than rebuild it.
    root = temp_repo / "proj"
    _materialise(root, {"other.py": "def x():\n    return 1\n"})
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    updater._rehydrated_module_qns.add("proj.stale")

    original = store.fetch_all

    def _boom(query: str, *args: object, **kwargs: object) -> list[dict[str, object]]:
        # Only the module query fails. Failing every query makes the
        # definition fetch raise first, and the function returns long before
        # it reaches the clear this test is about.
        if query is cs.CYPHER_ALL_MODULE_QNS:
            raise RuntimeError("module query failed")
        return original(query, *args, **kwargs)

    store.fetch_all = _boom  # type: ignore[method-assign]
    try:
        updater._is_full_build = True
        updater._rehydrate_registry_from_graph()
    finally:
        store.fetch_all = original  # type: ignore[method-assign]

    assert "proj.stale" in updater._rehydrated_module_qns


class _BufferingIngestor(_StatefulIngestor):
    # Production batches node writes and flushes at batch_size, so a module
    # parsed in the current run is NOT visible to a query issued mid-run. A
    # write-through fake hides every bug that depends on that.
    def __init__(self) -> None:
        super().__init__()
        self._pending: list[tuple[str, dict[str, object]]] = []

    def ensure_node_batch(self, label: str, properties: dict[str, object]) -> None:
        self._pending.append((label, properties))

    def flush_all(self) -> None:
        for label, properties in self._pending:
            super().ensure_node_batch(label, properties)
        self._pending = []


def test_a_cpp_interface_parsed_this_run_survives_rehydration(
    temp_repo: Path,
) -> None:
    # cpp_module_interfaces is rebuilt from the graph, but an interface this
    # run parsed is still unflushed when rehydration reads, so rebuilding
    # without it drops the IMPLEMENTS edge for an interface and an
    # implementation added together. An earlier attempt at this fix did
    # exactly that.
    root = temp_repo / "proj"
    _materialise(root, {"other.cpp": "int x() { return 1; }\n"})
    store = _BufferingIngestor()
    first = _updater(store, root, cs.SupportedLanguage.CPP)
    if cs.SupportedLanguage.CPP not in first.parsers:
        pytest.skip("cpp parser not available")
    first.run(force=True)
    store.flush_all()

    (root / "iface.cppm").write_text(
        "export module M;\nexport int f();\n", encoding="utf-8"
    )
    (root / "impl.cpp").write_text(
        "module M;\nint f() { return 1; }\n", encoding="utf-8"
    )
    _bump(root, "iface.cppm")
    _bump(root, "impl.cpp")

    second = _updater(store, root, cs.SupportedLanguage.CPP)
    dp = second.factory.definition_processor
    # The edge assertion below is true of a system where rehydration does
    # nothing: with the rebuild deleted the set is never cleared, so the
    # interface stays and the edge is minted anyway (issue #1725, measured).
    # A planted entry the graph does not hold separates the two: a real
    # rebuild drops it and keeps `proj.M`; a neutered pass keeps both; the
    # bare `clear()` this test was written against drops both.
    dp.cpp_module_interfaces.add("proj.STALE")
    rebuilt: list[frozenset[str]] = []
    real_rehydrate = second._rehydrate_registry_from_graph

    def _observe() -> None:
        real_rehydrate()
        rebuilt.append(frozenset(dp.cpp_module_interfaces))

    with patch.object(second, "_rehydrate_registry_from_graph", side_effect=_observe):
        second.run(force=False)
    store.flush_all()

    assert rebuilt, "rehydration never ran, so this test is not about it"
    assert "proj.STALE" not in rebuilt[-1], (
        "rehydration kept an interface the graph does not hold, so it did not "
        f"rebuild the set: {sorted(rebuilt[-1])}"
    )
    assert "proj.M" in rebuilt[-1], (
        "rehydration dropped the interface this run parsed but had not yet "
        f"flushed: {sorted(rebuilt[-1])}"
    )
    implements = [
        edge for edge in store.edges if edge[2] == cs.RelationshipType.IMPLEMENTS.value
    ]
    assert implements, "the interface was dropped before its impl resolved"


@pytest.mark.parametrize("reuse_updater", [False, True], ids=["fresh", "reused"])
def test_rehydration_readds_an_interface_held_only_by_the_graph(
    temp_repo: Path, reuse_updater: bool
) -> None:
    # The positive direction of the rebuild (issue #1736): an interface parsed
    # in an EARLIER run and untouched now is absent from
    # cpp_interfaces_parsed_this_run, so only the graph rows can put it back.
    # resolve_deferred_cpp_module_impls checks membership in
    # cpp_module_interfaces, so with the re-add gone an implementation unit
    # edited on its own loses its IMPLEMENTS edge. A sibling's fixture guard
    # ("the shape did not rehydrate") does catch the missing membership, but
    # nothing asserted the IMPLEMENTS edge on the incremental run or covered
    # a reused updater (measured: a resolver reading
    # cpp_interfaces_parsed_this_run instead of cpp_module_interfaces leaves
    # every other test in this file green and fails only this one). A fresh
    # updater has never parsed the interface at all; a reused one parsed it
    # last run, and the per-run reset of the companion set is what makes the
    # graph the only source there too.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "iface.cppm": "export module M;\nexport int f();\n",
            "impl.cpp": "module M;\nint f() { return 1; }\n",
        },
    )
    store = _StatefulIngestor()
    first = _updater(store, root, cs.SupportedLanguage.CPP)
    if cs.SupportedLanguage.CPP not in first.parsers:
        pytest.skip("cpp parser not available")
    first.run(force=True)
    clean_edges = [
        edge for edge in store.edges if edge[2] == cs.RelationshipType.IMPLEMENTS.value
    ]
    assert len(clean_edges) == 1, (
        f"fixture guard: the clean index did not resolve impl.cpp against M: {clean_edges}"
    )
    impl_label, impl_qn, _rel, iface_label, iface_qn = clean_edges[0]
    assert (str(iface_label), str(iface_qn)) == (
        cs.NodeLabel.MODULE_INTERFACE.value,
        "proj.M",
    ), clean_edges
    assert str(impl_label) == cs.NodeLabel.MODULE_IMPLEMENTATION.value, clean_edges

    second = first if reuse_updater else _updater(store, root, cs.SupportedLanguage.CPP)
    (root / "impl.cpp").write_text(
        "module M;\nint f() { return 2; }\n", encoding="utf-8"
    )
    _bump(root, "impl.cpp")
    dp = second.factory.definition_processor
    with patch.object(
        store, "ensure_relationship_batch", wraps=store.ensure_relationship_batch
    ) as spy:
        second.run(force=False)

    assert "proj.M" not in dp.cpp_interfaces_parsed_this_run, (
        "fixture guard: the interface was re-parsed, so the companion set and "
        "not the graph rows would have kept it"
    )
    assert "proj.M" in dp.cpp_module_interfaces, (
        "rehydration did not re-add the interface the graph holds: "
        f"{sorted(dp.cpp_module_interfaces)}"
    )
    # The same endpoints the clean index produced, not just any IMPLEMENTS
    # (CodeRabbit, #1754): the implementation unit as source, `proj.M` as the
    # ModuleInterface target.
    implements_now = [
        (
            str(call.args[0][0]),
            str(call.args[0][2]),
            str(call.args[2][0]),
            str(call.args[2][2]),
        )
        for call in spy.call_args_list
        if str(call.args[1]) == cs.RelationshipType.IMPLEMENTS.value
    ]
    assert implements_now == [
        (str(impl_label), str(impl_qn), str(iface_label), str(iface_qn))
    ], (
        "the edited implementation unit lost its IMPLEMENTS edge on the "
        f"incremental run, or emitted it between other endpoints: {implements_now}"
    )


def test_a_cpp_interface_the_graph_lost_is_forgotten(temp_repo: Path) -> None:
    # The mirror case: an interface another writer deleted must not linger,
    # or resolve_deferred_cpp_module_impls mints an IMPLEMENTS edge to a
    # ModuleInterface the graph no longer holds.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "iface.cppm": "export module M;\nexport int f();\n",
            "other.cpp": "int x() { return 1; }\n",
        },
    )
    store = _StatefulIngestor()
    first = _updater(store, root, cs.SupportedLanguage.CPP)
    if cs.SupportedLanguage.CPP not in first.parsers:
        pytest.skip("cpp parser not available")
    first.run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.CPP)
    (root / "other.cpp").write_text("int x() { return 2; }\n", encoding="utf-8")
    _bump(root, "other.cpp")
    updater.run(force=False)
    interfaces = updater.factory.definition_processor.cpp_module_interfaces
    assert "proj.M" in interfaces, "the shape did not rehydrate"

    removed = [key for key in store.nodes if key[0] == "ModuleInterface"]
    assert removed, "expected a ModuleInterface node to delete"
    for key in removed:
        del store.nodes[key]

    (root / "other.cpp").write_text("int x() { return 3; }\n", encoding="utf-8")
    _bump(root, "other.cpp")
    updater.run(force=False)

    assert "proj.M" not in updater.factory.definition_processor.cpp_module_interfaces


def test_a_parsed_interface_is_not_revived_on_a_later_run(temp_repo: Path) -> None:
    # cpp_interfaces_parsed_this_run exempts an interface from the rebuild
    # because its write may be unflushed. That exemption is scoped to ONE
    # run: without the per-run reset the qn stays in the companion set
    # forever and every later rebuild puts it back, which is the stale entry
    # this fix removes.
    #
    # The interface must be parsed by THIS updater, not a previous one --
    # with a fresh updater the companion set is empty and the reset cannot
    # be what saves it.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "iface.cppm": "export module M;\nexport int f();\n",
            "other.cpp": "int x() { return 1; }\n",
        },
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.CPP)
    if cs.SupportedLanguage.CPP not in updater.parsers:
        pytest.skip("cpp parser not available")
    updater.run(force=True)
    assert "proj.M" in updater.factory.definition_processor.cpp_module_interfaces

    removed = [key for key in store.nodes if key[0] == "ModuleInterface"]
    assert removed, "expected a ModuleInterface node to delete"
    for key in removed:
        del store.nodes[key]

    (root / "other.cpp").write_text("int x() { return 2; }\n", encoding="utf-8")
    _bump(root, "other.cpp")
    updater.run(force=False)

    assert "proj.M" not in updater.factory.definition_processor.cpp_module_interfaces


def test_seeded_module_qns_are_pruned_when_the_graph_loses_them(
    temp_repo: Path,
) -> None:
    # `_seed_module_qns_from_graph` only ever adds, so on a reused updater a
    # qn whose Module another writer deleted -- a second updater, a
    # delete_project then re-index, a run in another clone -- stayed in
    # module_qn_to_file_path for the life of the process. Unlike a rehydrated
    # qn it carries a real path, so it wins the Rust sub-scope arbitration.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    _updater(store, root, cs.SupportedLanguage.PYTHON).run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    qn_to_path = updater.factory.definition_processor.module_qn_to_file_path
    assert "proj.util" in qn_to_path, "the shape did not seed"

    # A qn the graph does not hold and whose file was never parsed here: what
    # an entry left over from an earlier project state looks like.
    qn_to_path["proj.ghost"] = root / "ghost.py"

    (root / "other.py").write_text("def x():\n    return 3\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)

    pruned = updater.factory.definition_processor.module_qn_to_file_path
    assert "proj.ghost" not in pruned
    assert "proj.ghost" not in updater.known_module_paths()
    # Entries the graph still holds survive.
    assert "proj.util" in pruned
    assert "proj.other" in pruned


def _seeded_updater(temp_repo: Path) -> tuple[GraphUpdater, _StatefulIngestor]:
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    _updater(store, root, cs.SupportedLanguage.PYTHON).run(force=True)
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)
    return updater, store


def test_an_empty_module_read_does_not_prune_the_seeded_map(
    temp_repo: Path,
) -> None:
    # No rows is "no verdict", not "the graph is empty". Pruning on an empty
    # read would drop every entry except the few this run re-parsed, which on
    # a one-file incremental run over a large repo is nearly the whole map.
    updater, store = _seeded_updater(temp_repo)
    before = dict(updater.factory.definition_processor.module_qn_to_file_path)
    assert len(before) > 1, before

    original = store.fetch_all

    def _empty(query: str, *args: object, **kwargs: object) -> list[dict[str, object]]:
        if query is cs.CYPHER_ALL_MODULE_PATHS_INTERNAL:
            return []
        return original(query, *args, **kwargs)

    store.fetch_all = _empty  # type: ignore[method-assign]
    try:
        updater._prune_stale_seeded_module_qns()
    finally:
        store.fetch_all = original  # type: ignore[method-assign]

    assert updater.factory.definition_processor.module_qn_to_file_path == before


def test_a_failed_module_read_does_not_prune_the_seeded_map(
    temp_repo: Path,
) -> None:
    # Same posture for a raising read, matching _rehydrate_registry_from_graph.
    updater, store = _seeded_updater(temp_repo)
    before = dict(updater.factory.definition_processor.module_qn_to_file_path)

    original = store.fetch_all

    def _boom(query: str, *args: object, **kwargs: object) -> list[dict[str, object]]:
        if query is cs.CYPHER_ALL_MODULE_PATHS_INTERNAL:
            raise RuntimeError("module path read failed")
        return original(query, *args, **kwargs)

    store.fetch_all = _boom  # type: ignore[method-assign]
    try:
        updater._prune_stale_seeded_module_qns()
    finally:
        store.fetch_all = original  # type: ignore[method-assign]

    assert updater.factory.definition_processor.module_qn_to_file_path == before


def test_a_file_parsed_this_run_survives_a_read_that_cannot_see_it(
    temp_repo: Path,
) -> None:
    # Production buffers node writes until a flush, so a file added this run
    # has no Module in the graph when the prune reads. Its entry must survive
    # on the strength of having been parsed, and keep its real PATH: the
    # pathless form loses the Rust sub-scope arbitration.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    _updater(store, root, cs.SupportedLanguage.PYTHON).run(force=True)

    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    (root / "fresh.py").write_text("def z():\n    return 1\n", encoding="utf-8")
    _bump(root, "fresh.py")

    original = store.fetch_all

    def _hide_fresh(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        rows = original(query, *args, **kwargs)
        if query is cs.CYPHER_ALL_MODULE_PATHS_INTERNAL:
            # What an unflushed write looks like from the prune's read.
            return [r for r in rows if r.get(cs.KEY_QUALIFIED_NAME) != "proj.fresh"]
        return rows

    store.fetch_all = _hide_fresh  # type: ignore[method-assign]
    try:
        updater.run(force=False)
    finally:
        store.fetch_all = original  # type: ignore[method-assign]

    qn_to_path = updater.factory.definition_processor.module_qn_to_file_path
    assert "proj.fresh" in qn_to_path, "a file parsed this run was pruned"
    assert updater.known_module_paths()["proj.fresh"].endswith("fresh.py")


def test_a_forced_run_also_prunes_the_seeded_map(temp_repo: Path) -> None:
    # A forced run re-parses every file but does NOT clear
    # module_qn_to_file_path, so on a reused updater a qn whose Module and
    # file are both gone survives the full rebuild. Gating the prune on
    # `not force` left exactly that hole.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    assert "proj.util" in updater.factory.definition_processor.module_qn_to_file_path

    removed = [key for key in store.nodes if key[1] == "proj.util"]
    assert removed, "expected a proj.util node to delete"
    for key in removed:
        del store.nodes[key]
    (root / "util.py").unlink()

    updater.run(force=True)

    pruned = updater.factory.definition_processor.module_qn_to_file_path
    assert "proj.util" not in pruned
    assert "proj.util" not in updater.known_module_paths()
    assert "proj.other" in pruned


def test_a_stale_qn_is_pruned_even_though_its_file_still_exists(
    temp_repo: Path,
) -> None:
    # The issue's own scenario: another writer deletes the Module while the
    # FILE stays on disk and this run does not re-parse it. The exemption is
    # "this run parsed it", not "the file exists" -- exempting on existence
    # is a plausible-looking fix that leaves exactly this entry behind, and
    # every other test here uses a ghost whose file is absent, so none of
    # them can tell the two apart.
    root = temp_repo / "proj"
    _materialise(
        root,
        {
            "util.py": "def helper():\n    return 1\n",
            "other.py": "def x():\n    return 1\n",
        },
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    assert "proj.util" in updater.factory.definition_processor.module_qn_to_file_path

    removed = [key for key in store.nodes if key[1] == "proj.util"]
    assert removed, "expected a proj.util node to delete"
    for key in removed:
        del store.nodes[key]
    assert (root / "util.py").exists(), "the file must survive for this shape"

    (root / "other.py").write_text("def x():\n    return 2\n", encoding="utf-8")
    _bump(root, "other.py")
    updater.run(force=False)

    pruned = updater.factory.definition_processor.module_qn_to_file_path
    assert "proj.util" not in pruned
    assert "proj.other" in pruned


JEDI_CORE = (
    "class Base:\n    def run(self):\n        return 1\n\n\ndef build():\n"
    "    return Base()\n"
)
JEDI_MAIN = (
    "from {module} import build\n\nhandlers = {{'a': build}}\n\n\ndef go():\n"
    "    return handlers['a']().run()\n"
)


def test_a_reused_updater_relearns_a_renamed_module_under_jedi(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The rel-path -> module qn memo the JEDI join reads rebuilds only when
    # its size differs from `module_qn_to_file_path`. A rename removes one
    # entry and adds one, so without an explicit reset the memo keeps
    # `base.py`, never learns `core.py`, and every fact targeting the
    # renamed file loses its declared location.
    pytest.importorskip("jedi")
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "PYTHON_FRONTEND", cs.PythonFrontend.JEDI)
    root = temp_repo / "proj"
    _materialise(
        root, {"base.py": JEDI_CORE, "main.py": JEDI_MAIN.format(module="base")}
    )
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.PYTHON)
    updater.run(force=True)
    (root / "base.py").rename(root / "core.py")
    (root / "main.py").write_text(JEDI_MAIN.format(module="core"), encoding="utf-8")
    _bump(root, "core.py")
    _bump(root, "main.py")
    updater.run(force=False)
    after = _snapshot(store, root)

    calls = {(e[1], e[4]) for e in after[1] if e[2] == cs.RelationshipType.CALLS.value}
    assert ("proj.main.go", "proj.core.Base.run") in calls
    assert after == _clean(
        temp_repo,
        {"core.py": JEDI_CORE, "main.py": JEDI_MAIN.format(module="core")},
        cs.SupportedLanguage.PYTHON,
    )


# Two resolver indexes are derived from module_qn_to_file_path and rebuilt
# lazily: the Go package index only when the map's size changes, the
# path-to-module index never. Removing a deleted module from the map (rather
# than leaving it, as before) means a rename keeps the size constant, so both
# must be reset with the other memos at the start of Pass 3.
GO_BEFORE = {
    "pkg/types.go": "package pkg\n\ntype T struct{}\n",
    "pkg/methods.go": "package pkg\n\nfunc (t T) M() int { return helper() }\n",
    "pkg/util.go": "package pkg\n\nfunc helper() int { return 1 }\n",
}
GO_AFTER = {
    "pkg/types.go": GO_BEFORE["pkg/types.go"],
    "pkg/methods.go": GO_BEFORE["pkg/methods.go"],
    "pkg/helpers.go": GO_BEFORE["pkg/util.go"],
}


def _calls(snapshot: Snapshot) -> set[tuple[str, str]]:
    return {
        (e[1], e[4]) for e in snapshot[1] if e[2] == cs.RelationshipType.CALLS.value
    }


def test_a_reused_updater_resolves_a_go_call_after_a_same_package_rename(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, GO_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    (root / "pkg" / "util.go").rename(root / "pkg" / "helpers.go")
    _bump(root, "pkg/helpers.go")
    updater.run(force=False)
    after = _snapshot(store, root)

    # A stale package index lists the deleted `proj.pkg.util`, the receiver
    # lookup raises on it, and methods.go then contributes no CALLS at all.
    calls = _calls(after)
    assert ("proj.pkg.types.T.M", "proj.pkg.helpers.helper") in calls
    assert calls == _calls(_clean(temp_repo, GO_AFTER, cs.SupportedLanguage.GO))


JS_ADD_AFTER = {
    **JS_BEFORE,
    "util.ts": (
        "export function helper2(): number { return 1; }\n"
        "export function helper2b(): number { return helper2(); }\n"
    ),
}


def test_a_reused_updater_keys_a_new_same_stem_module_by_its_own_qn(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, JS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    (root / "util.ts").write_text(JS_ADD_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    # A path index built on the first run has no entry for util.ts, so its
    # calls were attributed to the JS module's qn (`proj.util.helper`).
    calls = _calls(after)
    assert ("proj.util.ts.helper2b", "proj.util.ts.helper2") in calls
    assert after == _clean(temp_repo, JS_ADD_AFTER, cs.SupportedLanguage.JS)


# The Go, Java and C# semantic-join engines memoise rel-path -> module qn
# and rebuild only when module_qn_to_file_path changes size, so they go
# stale on a rename exactly as the resolver's own memos did. Modelled on Go
# with a synthetic fact standing in for the go/types frontend.
GO_IFACE = (
    "package sample\n\ntype Handler interface{ Handle() string }\n\n"
    "func Run(h Handler) {\n\t_ = h.Handle()\n}\n"
)
GO_A = (
    'package sample\n\ntype A struct{}\n\nfunc (a A) Handle() string { return "a" }\n'
)
GO_B = (
    'package sample\n\ntype B struct{}\n\nfunc (b B) Handle() string { return "b" }\n'
)
GO_MOD = "module example.com/p\n\ngo 1.23\n"
GO_FACT_BEFORE = {"go.mod": GO_MOD, "iface.go": GO_IFACE, "a.go": GO_A, "b.go": GO_B}
GO_FACT_AFTER = {"go.mod": GO_MOD, "iface.go": GO_IFACE, "a.go": GO_A, "c.go": GO_B}
GO_CALL_KEY = ("iface.go", 6, len("\t_ = h."), "Handle")
GO_TARGET = (5, len("func (b B) "))


def _go_facts(target_file: str) -> GoSemanticFacts:
    return GoSemanticFacts(
        call_sites={GO_CALL_KEY: GoCallSite("Handle", target_file, *GO_TARGET)},
        external_sites=set(),
        implements=[],
    )


def test_a_reused_updater_joins_a_go_fact_against_the_renamed_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {"facts": _go_facts("b.go")}
    monkeypatch.setattr(gu.settings, "GO_FRONTEND", cs.GoFrontend.GOTYPES)
    monkeypatch.setattr(go_fe, "go_frontend_available", lambda: True)
    monkeypatch.setattr(go_fe, "run_go_frontend", lambda repo_path: holder["facts"])

    root = temp_repo / "proj"
    _materialise(root, GO_FACT_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    # Positive control: the fact, not the heuristic, decides the first run.
    assert ("proj.iface.Run", "proj.b.B.Handle") in first, first
    assert ("proj.iface.Run", "proj.a.A.Handle") not in first, first

    (root / "b.go").rename(root / "c.go")
    _bump(root, "c.go")
    holder["facts"] = _go_facts("c.go")
    updater.run(force=False)
    after = _calls(_snapshot(store, root))

    # With the memo stale the fact's target resolves to nothing and the
    # heuristic binds Run to A.Handle instead.
    assert ("proj.iface.Run", "proj.c.B.Handle") in after, after
    assert after == _calls(_clean(temp_repo, GO_FACT_AFTER, cs.SupportedLanguage.GO))


# Java twin of the Go test above: same declared_location join, same
# rel-path memo on the Java engine. The fact is built after Pass 2 from the
# registered span of B.handle in whichever file currently holds it.
JAVA_HANDLER = "public interface Handler {\n    String handle();\n}\n"
JAVA_A = (
    "public class A implements Handler {\n"
    '    public String handle() { return "a"; }\n}\n'
)
JAVA_B = (
    "public class B implements Handler {\n"
    '    public String handle() { return "b"; }\n}\n'
)
JAVA_RUN = (
    "public class Run {\n    public static void run(Handler h) {\n"
    "        h.handle();\n    }\n}\n"
)
JAVA_FACT_BEFORE = {
    "Handler.java": JAVA_HANDLER,
    "A.java": JAVA_A,
    "B.java": JAVA_B,
    "Run.java": JAVA_RUN,
}
JAVA_FACT_AFTER = {
    "Handler.java": JAVA_HANDLER,
    "A.java": JAVA_A,
    "C.java": JAVA_B,
    "Run.java": JAVA_RUN,
}
JAVA_CALL_KEY = ("Run.java", 3, len("        h."), "handle")


def test_a_reused_updater_joins_a_java_fact_against_the_renamed_module(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = temp_repo / "proj"
    holder: dict[str, object] = {"target_file": "B.java"}

    def facts(repo_path: Path) -> JavaSemanticFacts:
        updater = holder["updater"]
        assert isinstance(updater, GraphUpdater)
        target_file = str(holder["target_file"])
        dp = updater.factory.definition_processor
        target_path = root / target_file
        modules = {
            qn for qn, path in dp.module_qn_to_file_path.items() if path == target_path
        }
        spans = sorted(
            (line, col)
            for (mod, line, col), loc in dp.function_locations.items()
            if mod in modules and ".B.handle" in loc.qualified_name
        )
        assert spans, (modules, sorted(dp.function_locations))
        line, col = spans[0]
        return JavaSemanticFacts(
            call_sites={JAVA_CALL_KEY: JavaCallSite("handle", target_file, line, col)},
            external_sites=set(),
        )

    monkeypatch.setattr(gu.settings, "JAVA_FRONTEND", cs.JavaFrontend.JAVAC)
    monkeypatch.setattr(java_fe, "java_frontend_available", lambda: True)
    monkeypatch.setattr(java_fe, "run_java_frontend", facts)

    _materialise(root, JAVA_FACT_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JAVA)
    holder["updater"] = updater
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    # Positive control: the fact binds the call to B.handle on the first run.
    assert any(t.startswith("proj.B.B.handle") for _, t in first), first

    (root / "B.java").rename(root / "C.java")
    _bump(root, "C.java")
    holder["target_file"] = "C.java"
    updater.run(force=False)
    after = _calls(_snapshot(store, root))

    assert any(
        s.startswith("proj.Run.Run.run") and t.startswith("proj.C.B.handle")
        for s, t in after
    ), after
    assert after == _calls(
        _clean(temp_repo, JAVA_FACT_AFTER, cs.SupportedLanguage.JAVA)
    )


# A same-stem replacement takes over the deleted file's module qn, and the
# CommonJS export registry keys on that qn: dropping the deleted file's state
# a second time after the parse would strip the replacement's entry and the
# whole-module alias call below would lose its edge.
CJS_BEFORE = {
    "util.js": "module.exports = function helper() { return 1; };\n",
    "main.js": "const f = require('./util');\nfunction go() { return f(); }\n",
}
CJS_AFTER = {
    "util.ts": CJS_BEFORE["util.js"],
    "main.js": CJS_BEFORE["main.js"],
}


def test_a_reused_updater_keeps_a_same_stem_replacements_commonjs_export(
    temp_repo: Path,
) -> None:
    root = temp_repo / "proj"
    _materialise(root, CJS_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.JS)
    updater.run(force=True)
    first = _calls(_snapshot(store, root))
    assert ("proj.main.go", "proj.util.helper") in first, first

    (root / "util.js").unlink()
    (root / "util.ts").write_text(CJS_AFTER["util.ts"], encoding="utf-8")
    _bump(root, "util.ts")
    updater.run(force=False)
    after = _snapshot(store, root)

    assert ("proj.main.go", "proj.util.helper") in _calls(after), _calls(after)
    assert after == _clean(temp_repo, CJS_AFTER, cs.SupportedLanguage.JS)


def _methods(snapshot: Snapshot) -> set[str]:
    return {uid for (label, uid) in snapshot[0] if label == cs.NodeLabel.METHOD.value}


def test_re_parsing_a_go_file_does_not_duplicate_a_sibling_keyed_method(
    temp_repo: Path,
) -> None:
    """A Go method is keyed under its RECEIVER TYPE's module, not its own file.

    `func (t T) M()` in methods.go registers as `proj.pkg.types.T.M`, because
    `T` is declared in types.go. `remove_file_from_state(methods.go)` sweeps
    by the qn prefixes methods.go recorded (`proj.pkg.methods`), which that qn
    does not match, so the first run's Method survives the cleanup and the
    re-parse registers a second one beside it as `proj.pkg.types.T.M@3`.

    Asserted on Method NODES rather than on CALLS edges: the rename test above
    compares calls only, and calls alone are satisfied by a graph carrying
    both the stale and the fresh method (issue #1659).
    """
    root = temp_repo / "proj"
    _materialise(root, GO_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    # Renaming util.go joins methods.go to the run as a DEPENDENT, so it is
    # re-parsed. Touching methods.go instead would not do: the hash cache sees
    # identical content and skips the file, so nothing is re-parsed and the
    # duplicate never forms -- a green test about a run that did not happen.
    (root / "pkg" / "util.go").rename(root / "pkg" / "helpers.go")
    _bump(root, "pkg/helpers.go")
    updater.run(force=False)
    after = _snapshot(store, root)

    clean = _clean(temp_repo, GO_AFTER, cs.SupportedLanguage.GO)
    assert _methods(after) == _methods(clean)
    assert _methods(after) == {"proj.pkg.types.T.M"}


GO_TYPE_RENAMED = {
    "pkg/kinds.go": GO_BEFORE["pkg/types.go"],
    "pkg/methods.go": GO_BEFORE["pkg/methods.go"],
    "pkg/util.go": GO_BEFORE["pkg/util.go"],
}


def test_renaming_the_go_file_holding_a_receiver_type_keeps_the_method_calls(
    temp_repo: Path,
) -> None:
    """Renaming types.go must not strand the methods bound to the type it held.

    `func (t T) M()` lives in methods.go but keys under the module that
    declares `T`. Renaming types.go to kinds.go moves that module, and on a
    reused updater the method's CALLS edge disappeared entirely rather than
    rebinding under the new module (issue #1660).

    Compared against a clean index of the renamed tree rather than a hardcoded
    qn: whether the method should now key under `proj.pkg.kinds` is the clean
    index's answer to give, and pinning my own guess would make this test
    agree with whatever I happened to build.

    Sits beside the #1659 test deliberately: both start from the same fixture
    and rename one file, and the pair is what shows the two defects are
    distinct -- that one removes stale state, this one nominates a file to
    re-parse.
    """
    root = temp_repo / "proj"
    _materialise(root, GO_BEFORE)
    store = _StatefulIngestor()
    updater = _updater(store, root, cs.SupportedLanguage.GO)
    updater.run(force=True)
    (root / "pkg" / "types.go").rename(root / "pkg" / "kinds.go")
    _bump(root, "pkg/kinds.go")
    updater.run(force=False)
    after = _snapshot(store, root)

    clean = _clean(temp_repo, GO_TYPE_RENAMED, cs.SupportedLanguage.GO)
    assert _calls(after) == _calls(clean)
    assert after == clean
