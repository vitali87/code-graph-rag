"""An incremental run lists the modules a fresh index of the tree lists.

A bodied inline `mod inner { ... }` is a Module node in the graph, written
with its file's path. A fresh index knows it only as a scope inside its
file's module, so the project index hand-off (`index.txt`) has no `module`
line for it. An incremental run read every Module back from the graph and
registered the inline `mod` under its file's path as a module of its own, so
its index listed one (issue #3810). The run now registers the file's module
alone and, as before, forgets the inline `mod`s with their file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.config import settings
from codebase_rag.tests.test_graph_updater_index_engine import _write
from codebase_rag.tests.test_handoff import (
    _FILES,
    _FRONTENDS,
    _INDEX,
    _embedded_graph,
    _lines,
    _updater,
)
from codebase_rag.tests.test_handoff_incremental_imports import (
    _fresh_index,
    _imports_keys,
    _sync,
)

# A bodied inline `mod` with a nested one, and `a::b` declared twice behind a
# cfg: inline in `a/mod.rs` and as the file `a/b.rs`, whose Module node the
# inline twin shares. The edit is to a file nothing in the crate reaches.
TREE = {
    "crab/Cargo.toml": (
        '[package]\nname = "crab"\nversion = "0.1.0"\n\n[features]\ninline = []\n'
    ),
    "crab/src/util.rs": "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n",
    "crab/src/lib.rs": (
        "pub mod a;\npub mod util;\n\n"
        "mod inner {\n    use crate::util::helper;\n\n"
        "    pub fn run() -> i32 {\n        helper(1)\n    }\n\n"
        "    pub mod deeper {\n"
        "        pub fn depth() -> i32 {\n            super::run()\n        }\n"
        "    }\n}\n"
    ),
    "crab/src/a/mod.rs": (
        '#[cfg(feature = "inline")]\n'
        "pub mod b {\n    pub fn twin() -> i32 {\n        1\n    }\n}\n\n"
        '#[cfg(not(feature = "inline"))]\n'
        "pub mod b;\n"
    ),
    "crab/src/a/b.rs": "pub fn twin() -> i32 {\n    2\n}\n",
    "other.py": "def lonely():\n    return 1\n",
}
EDIT = ("other.py", "def lonely():\n    return 2\n")
# The file modules alone: no inline `mod`, and the twin's file under its qn.
MODULES = {
    "demo.crab.src.lib",
    "demo.crab.src.util",
    "demo.crab.src.a",
    "demo.crab.src.a.b",
    "demo.other",
}
INLINE_MODS = {"demo.crab.src.lib.inner", "demo.crab.src.lib.inner.deeper"}


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


def _modules(index: list[str]) -> set[str]:
    return {line.split("\t")[1] for line in index if line.startswith("module\t")}


@pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
def test_it_lists_no_module_for_an_inline_mod_as_a_fresh_index_does(
    tmp_path: Path, reused: bool
) -> None:
    repo = _write(tmp_path / "repo", TREE)
    synced = tmp_path / "synced"
    _sync(repo, tmp_path / "graph.sqlite", EDIT, reused=reused, handoffs=synced)
    fresh = _fresh_index(repo, tmp_path)

    edited, _text = EDIT
    assert f'reparse\t{edited}\t{{"reason":"changed"}}' in _lines(synced / _FILES)
    assert _modules(fresh) == MODULES
    # The inline `mod`'s own `use` map, which both kinds of run list.
    assert "demo.crab.src.lib.inner" in _imports_keys(fresh)
    assert _lines(synced / _INDEX) == fresh


@pytest.mark.parametrize("deleted", [False, True], ids=["edited", "deleted"])
def test_a_reingest_forgets_the_inline_mods_of_a_file_it_drops(
    tmp_path: Path, deleted: bool
) -> None:
    # The module qns read back from the graph name the inline `mod`s, and a
    # file's state drop took them out with the map entries the seed gave
    # them. Left out of the map, they must still go with their file, or a
    # scoped re-ingest on a new updater (the MCP tool, the watcher) keeps
    # offering a removed `mod` to deferred resolution as a live module.
    repo = _write(tmp_path / "repo", TREE)
    lib = repo / "crab/src/lib.rs"
    with _embedded_graph(tmp_path / "graph.sqlite") as ingestor:
        _updater(repo, ingestor, state_dir=None, handoff_dir=None).run()
        updater = _updater(repo, ingestor, state_dir=None, handoff_dir=None)
        updater._hydrate_for_reingest()
        assert INLINE_MODS <= updater._rehydrated_module_qns, "nothing rehydrated"
        if deleted:
            lib.unlink()
        else:
            lib.write_text("pub mod a;\npub mod util;\n", encoding="utf-8")
        updater.reingest([lib])

        assert not INLINE_MODS & updater.known_module_paths().keys()
