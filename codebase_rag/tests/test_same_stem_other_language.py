"""A same-stem file in another language takes no name from its sibling (#2586).

Module qns drop the extension, so `util.py` and `util.js` both derive
`proj.util`. The first file in walk order used to keep that bare name and the
other was suffixed, so adding `util.js` beside an indexed `util.py` renamed the
Python module to `proj.util.py` and handed `proj.util.helper` to the JS
function: every stored name (a gloss, an MCP client, a saved query) went on
resolving, silently, to code in another language. The Python importer's CALLS
and IMPORTS edges followed the name to the JS file too, and on an incremental
sync the added file lost its own CALLS until a fresh index.

Qualified names stay a function of the tree (a fresh index and an incremental
sync agree, issues #1569 and #2022), so whichever of the two arrives second
changes the tree the other is named in. Only one rule never hands a name to a
different file: a stem shared across language families has no bare module, and
each file carries its own extension (`proj.util.py`, `proj.util.js`). Importers
still write the bare name and land on the file of their own language.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PY_UTIL = "def helper():\n    return 1\n\n\ndef main():\n    return helper()\n"
PY_APP = "from util import helper\n\n\ndef run():\n    return helper()\n"
JS_UTIL = (
    "function jsHelper() {\n  return 2;\n}\n"
    "function helper() {\n  return jsHelper() + [1, 2].map(jsHelper).length;\n}\n"
    "module.exports = { helper };\n"
)
JS_APP = (
    "const { helper } = require('./util');\nfunction go() {\n  return helper();\n}\n"
)
TS_UTIL = "export function helper(): number {\n  return 3;\n}\n"
GO_UTIL = "package util\n\nfunc Helper() int {\n\treturn 4\n}\n"

_ABSOLUTE = {cs.NodeLabel.FOLDER.value, cs.NodeLabel.PACKAGE.value}
_DEFINITIONS = {
    cs.NodeLabel.FUNCTION.value,
    cs.NodeLabel.METHOD.value,
    cs.NodeLabel.CLASS.value,
    cs.NodeLabel.MODULE.value,
}
Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


class _BufferingStore(_StatefulIngestor):
    """The emulator, with node writes landing only at flush.

    The real ingestor batches writes and a mid-run read does not see the
    unflushed ones, so a registration this run made and then lost is not
    quietly read back from the graph as it is from a write-through fake.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending: list[tuple[str, dict[str, object]]] = []

    def ensure_node_batch(self, label: str, properties: dict[str, object]) -> None:
        self._pending.append((label, properties))

    def flush_all(self) -> None:
        for label, properties in self._pending:
            super().ensure_node_batch(label, properties)
        self._pending = []


def _write(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _updater(
    store: _StatefulIngestor, root: Path, exclude: frozenset[str] | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
        exclude_paths=exclude,
    )


def _index(
    store: _StatefulIngestor,
    root: Path,
    force: bool,
    exclude: frozenset[str] | None = None,
) -> None:
    _updater(store, root, exclude).run(force=force)
    store.flush_all()


def _bump(root: Path, *rels: str) -> None:
    # Past the cache's mtime, so the next run sees the change even within
    # the filesystem's timestamp resolution.
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    for rel in (*rels, "."):
        os.utime(root / rel, (cache_mtime + 1, cache_mtime + 1))


def _names_by_path(store: _StatefulIngestor) -> dict[str, str]:
    """Qualified name -> the file of the definition or module holding it."""
    return {
        str(uid): str(props.get(cs.KEY_PATH))
        for (label, uid), props in store.nodes.items()
        if label in _DEFINITIONS
    }


def _edges(store: _StatefulIngestor, rel: cs.RelationshipType) -> set[tuple[str, str]]:
    paths = _names_by_path(store)
    return {
        (
            paths.get(str(fv), "?") + ":" + str(fv),
            paths.get(str(tv), "?") + ":" + str(tv),
        )
        for (_fl, fv, r, _tl, tv) in store.edges
        if r == rel
    }


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


def _clean(tmp_path: Path, files: dict[str, str]) -> Snapshot:
    root = tmp_path / "clean" / "proj"
    _write(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True)
    return _snapshot(store, root)


def _module_qns(store: _StatefulIngestor) -> dict[str, str]:
    """Module path -> its qualified name."""
    return {
        str(props.get(cs.KEY_PATH)): str(uid)
        for (label, uid), props in store.nodes.items()
        if label == cs.NodeLabel.MODULE.value
    }


@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "reused"])
def test_adding_a_sibling_in_another_language_hands_it_no_existing_name(
    tmp_path: Path, reuse: bool
) -> None:
    root = tmp_path / "proj"
    _write(root, {"util.py": PY_UTIL, "app.py": PY_APP})
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    held = {qn for qn, path in _names_by_path(store).items() if path == "util.py"}
    assert "proj.util.helper" in held, sorted(held)

    _write(root, {"util.js": JS_UTIL})
    _bump(root, "util.js")
    (updater if reuse else _updater(store, root)).run(force=False)

    after = _names_by_path(store)
    taken = {qn: after[qn] for qn in held if after.get(qn, "util.py") != "util.py"}
    assert not taken, f"util.py's names now name another file's code: {taken}"
    assert _snapshot(store, root) == _clean(
        tmp_path, {"util.py": PY_UTIL, "app.py": PY_APP, "util.js": JS_UTIL}
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (("util.py", PY_UTIL), ("util.js", JS_UTIL)),
        (("util.js", JS_UTIL), ("util.py", PY_UTIL)),
        (("util.py", PY_UTIL), ("util.ts", TS_UTIL)),
        (("util.go", GO_UTIL), ("util.py", PY_UTIL)),
    ],
    ids=["py-then-js", "js-then-py", "py-then-ts", "go-then-py"],
)
def test_a_cross_language_pair_is_named_the_same_whichever_file_came_first(
    tmp_path: Path, first: tuple[str, str], second: tuple[str, str]
) -> None:
    root = tmp_path / "proj"
    _write(root, dict([first]))
    store = _StatefulIngestor()
    _index(store, root, force=True)
    assert _module_qns(store) == {first[0]: "proj.util"}, "alone, a file is bare"

    _write(root, dict([second]))
    _bump(root, second[0])
    _index(store, root, force=False)

    modules = _module_qns(store)
    assert modules == {
        first[0]: f"proj.{first[0]}",
        second[0]: f"proj.{second[0]}",
    }, modules
    assert _snapshot(store, root) == _clean(tmp_path, dict([first, second]))


@pytest.mark.parametrize("incremental", [False, True], ids=["full", "incremental"])
def test_callers_and_importers_keep_to_their_own_languages_file(
    tmp_path: Path, incremental: bool
) -> None:
    root = tmp_path / "proj"
    python_side = {"util.py": PY_UTIL, "app.py": PY_APP}
    js_side = {"util.js": JS_UTIL, "jsapp.js": JS_APP}
    store = _StatefulIngestor()
    if incremental:
        _write(root, python_side)
        _index(store, root, force=True)
        _write(root, js_side)
        _bump(root, *js_side)
    else:
        _write(root, {**python_side, **js_side})
    _index(store, root, force=not incremental)

    calls = _edges(store, cs.RelationshipType.CALLS)
    assert ("app.py:proj.app.run", "util.py:proj.util.py.helper") in calls, calls
    assert ("jsapp.js:proj.jsapp.go", "util.js:proj.util.js.helper") in calls, calls
    assert not any(
        src.startswith("app.py:") and dst.startswith("util.js:") for src, dst in calls
    ), calls
    imports = _edges(store, cs.RelationshipType.IMPORTS)
    assert ("app.py:proj.app", "util.py:proj.util.py") in imports, imports
    assert ("jsapp.js:proj.jsapp", "util.js:proj.util.js") in imports, imports
    assert not any(
        src.startswith("app.py:") and "util.js" in dst for src, dst in imports
    )


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
@pytest.mark.parametrize("step", ["add", "delete"])
def test_the_watcher_path_names_the_pair_as_a_clean_index_does(
    tmp_path: Path, fresh_updater: bool, step: str
) -> None:
    # `reingest` is what the watcher and the MCP server call per event; it
    # reconciles same-stem siblings on its own path (issue #1569).
    root = tmp_path / "proj"
    python_side = {"util.py": PY_UTIL, "app.py": PY_APP}
    _write(root, python_side if step == "add" else {**python_side, "util.js": JS_UTIL})
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    if step == "add":
        _write(root, {"util.js": JS_UTIL})
        changed, deleted, final = ["util.js"], [], {**python_side, "util.js": JS_UTIL}
    else:
        (root / "util.js").unlink()
        changed, deleted, final = [], ["util.js"], python_side
    if fresh_updater:
        updater = _updater(store, root)

    updater.reingest(changed, deleted=deleted)

    assert _snapshot(store, root) == _clean(tmp_path, final)
    expected = "proj.util.py.helper" if step == "add" else "proj.util.helper"
    assert ("app.py:proj.app.run", f"util.py:{expected}") in _edges(
        store, cs.RelationshipType.CALLS
    )


@pytest.mark.parametrize(
    ("base", "added", "call"),
    [
        (
            {"util.py": PY_UTIL},
            ("util.js", JS_UTIL),
            ("proj.util.js.helper", "proj.util.js.jsHelper"),
        ),
        (
            {"util.h": "static inline int twice(int a) { return a * 2; }\n"},
            (
                "util.c",
                '#include "util.h"\n'
                "static int helper(int a) { return twice(a); }\n"
                "int add(int a, int b) { return helper(a) + b; }\n",
            ),
            ("proj.util.add", "proj.util.helper"),
        ),
    ],
    ids=["other-language", "same-language"],
)
def test_the_added_sibling_keeps_its_calls_after_an_incremental_sync(
    tmp_path: Path,
    base: dict[str, str],
    added: tuple[str, str],
    call: tuple[str, str],
) -> None:
    # Re-parsing the survivor swept the registry under its path-derived qn,
    # which by then named the sibling parsed just before it; the sibling's
    # unflushed definitions could not be read back, so its calls resolved
    # to nothing until a fresh index (reported on #2586).
    root = tmp_path / "proj"
    _write(root, base)
    store = _BufferingStore()
    _index(store, root, force=True)
    _write(root, dict([added]))
    _bump(root, added[0])
    _index(store, root, force=False)

    calls = {
        (str(fv), str(tv)) for (_fl, fv, r, _tl, tv) in store.edges if r == "CALLS"
    }
    assert call in calls, sorted(calls)

    fresh = _BufferingStore()
    clean_root = tmp_path / "clean" / "proj"
    _write(clean_root, {**base, added[0]: added[1]})
    _index(fresh, clean_root, force=True)
    clean_calls = {
        (str(fv), str(tv)) for (_fl, fv, r, _tl, tv) in fresh.edges if r == "CALLS"
    }
    assert calls == clean_calls


# Negative tests: what the change must leave alone.


@pytest.mark.parametrize(
    ("name", "text"),
    [("util.py", PY_UTIL), ("util.js", JS_UTIL)],
    ids=["python", "javascript"],
)
def test_a_lone_file_keeps_its_bare_name(tmp_path: Path, name: str, text: str) -> None:
    root = tmp_path / "proj"
    _write(root, {name: text})
    store = _StatefulIngestor()
    _index(store, root, force=True)

    assert _module_qns(store) == {name: "proj.util"}
    assert "proj.util.helper" in _names_by_path(store)


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {"util.py": PY_UTIL, "util/__init__.py": "def pkg():\n    return 0\n"},
            {"util.py": "proj.util", "util/__init__.py": "proj.util.py"},
        ),
        (
            {"util.h": "int util(void);\n", "util.c": "int util(void) { return 1; }\n"},
            {"util.c": "proj.util", "util.h": "proj.util.h"},
        ),
        (
            {"util.js": JS_UTIL, "util.ts": TS_UTIL},
            {"util.js": "proj.util", "util.ts": "proj.util.ts"},
        ),
    ],
    ids=["python-module-and-package", "c-source-and-header", "js-and-ts"],
)
def test_a_same_language_pair_keeps_the_first_walked_rule(
    tmp_path: Path, files: dict[str, str], expected: dict[str, str]
) -> None:
    # One language family shares one module system: a header and its source,
    # a .js and its .ts, are reached through the one extension-less name, so
    # the first file in walk order keeps it, as before.
    root = tmp_path / "proj"
    _write(root, files)
    store = _StatefulIngestor()
    _index(store, root, force=True)

    assert _module_qns(store) == expected


@pytest.mark.parametrize(
    ("sibling", "exclude"),
    [(("util.md", "# util\n"), None), (("util.js", JS_UTIL), frozenset({"util.js"}))],
    ids=["not-a-source-language", "excluded"],
)
def test_a_sibling_that_is_not_indexed_as_a_module_renames_nothing(
    tmp_path: Path, sibling: tuple[str, str], exclude: frozenset[str] | None
) -> None:
    root = tmp_path / "proj"
    _write(root, {"util.py": PY_UTIL, sibling[0]: sibling[1]})
    store = _StatefulIngestor()
    _index(store, root, force=True, exclude=exclude)

    assert _module_qns(store).get("util.py") == "proj.util"


def test_an_import_of_a_real_submodule_under_the_stem_is_left_alone(
    tmp_path: Path,
) -> None:
    # `util/sub.py` is its own module (`proj.util.sub`) whatever happens to
    # `util.py`; re-rooting every name under `proj.util` would send the import
    # to a `proj.util.py.sub` that does not exist.
    root = tmp_path / "proj"
    _write(
        root,
        {
            "util.py": PY_UTIL,
            "util.js": JS_UTIL,
            "util/sub.py": "def deep():\n    return 5\n",
            "app.py": "from util.sub import deep\n\n\ndef run():\n    return deep()\n",
        },
    )
    store = _StatefulIngestor()
    _index(store, root, force=True)

    calls = _edges(store, cs.RelationshipType.CALLS)
    assert ("app.py:proj.app.run", "util/sub.py:proj.util.sub.deep") in calls, calls
    imports = _edges(store, cs.RelationshipType.IMPORTS)
    assert ("app.py:proj.app", "util/sub.py:proj.util.sub") in imports, imports


@pytest.mark.parametrize("reuse", [False, True], ids=["fresh", "reused"])
def test_removing_the_other_language_sibling_restores_the_original_graph(
    tmp_path: Path, reuse: bool
) -> None:
    root = tmp_path / "proj"
    original = {"util.py": PY_UTIL, "app.py": PY_APP}
    _write(root, original)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    before = _snapshot(store, root)

    _write(root, {"util.js": JS_UTIL})
    _bump(root, "util.js")
    (updater if reuse else _updater(store, root)).run(force=False)
    assert _module_qns(store).get("util.py") == "proj.util.py"

    (root / "util.js").unlink()
    _bump(root)
    (updater if reuse else _updater(store, root)).run(force=False)

    after = _snapshot(store, root)
    assert after == before, {
        "extra": sorted(map(str, after[0] - before[0] | after[1] - before[1])),
        "missing": sorted(map(str, before[0] - after[0] | before[1] - after[1])),
    }
