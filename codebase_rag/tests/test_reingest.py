# Scoped re-ingest (issue #1524): GraphUpdater.reingest(paths) re-parses only
# the named files and the files that depend on them, resolves calls in that
# set only, and restores every other inbound edge verbatim. The promise is
# that the graph afterwards equals a clean full index of the same tree, so a
# clean index is the oracle for every edit below.
from __future__ import annotations

import hashlib
import json
import os
import random
from collections.abc import Callable, Mapping
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import (
    CYPHER_PROJECT_MODULES,
    CYPHER_PROJECT_PY_MODULES,
    CYPHER_PROJECT_ROUTE_HANDLERS,
    GraphUpdater,
    ReingestAborted,
)
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater
from evals.cgr_graph import _StatefulIngestor

PROJECT = "reingest_fixture"

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper():\n    return 1\n\n\ndef other():\n    return 2\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"
    ),
    "pkg/unrelated.py": "def alone():\n    return 3\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
}

# Each edit mutates the tree and returns (changed_paths, deleted_paths) the
# way an agent or a watcher would report them.
Edit = Callable[[Path], tuple[list[str], list[str]]]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def edit_neutral_comment(root: Path) -> tuple[list[str], list[str]]:
    path = root / "pkg/util.py"
    path.write_text(path.read_text() + "# trailing comment\n", encoding="utf-8")
    return ["pkg/util.py"], []


def edit_add_function_and_call(root: Path) -> tuple[list[str], list[str]]:
    _write(
        root, "pkg/util.py", FIXTURE["pkg/util.py"] + "\n\ndef fresh():\n    return 4\n"
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper, fresh\n\n\ndef run():\n    fresh()\n    return helper()\n",
    )
    return ["pkg/util.py", "pkg/app.py"], []


def edit_remove_call(root: Path) -> tuple[list[str], list[str]]:
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return 0\n",
    )
    return ["pkg/app.py"], []


def edit_rename_callee(root: Path) -> tuple[list[str], list[str]]:
    # app.py still calls `helper`, which no longer exists: its CALLS edge must
    # go, and only a re-parse of app.py (a dependent) can drop it.
    _write(
        root,
        "pkg/util.py",
        "def helper2():\n    return 1\n\n\ndef other():\n    return 2\n",
    )
    return ["pkg/util.py"], []


def edit_delete_file(root: Path) -> tuple[list[str], list[str]]:
    (root / "pkg/unrelated.py").unlink()
    return [], ["pkg/unrelated.py"]


def edit_create_file(root: Path) -> tuple[list[str], list[str]]:
    _write(
        root,
        "pkg/extra.py",
        "from pkg.util import other\n\n\ndef extra():\n    return other()\n",
    )
    return ["pkg/extra.py"], []


def edit_caller_of_caller(root: Path) -> tuple[list[str], list[str]]:
    # main.py depends on app.py, which depends on util.py: only one level of
    # dependents re-parses, and main.py's edge into app.py must survive.
    _write(
        root,
        "pkg/util.py",
        "def helper():\n    return 10\n\n\ndef other():\n    return 2\n",
    )
    return ["pkg/util.py"], []


SINGLE_EDITS: dict[str, Edit] = {
    "neutral_comment": edit_neutral_comment,
    "add_function_and_call": edit_add_function_and_call,
    "remove_call": edit_remove_call,
    "rename_callee": edit_rename_callee,
    "delete_file": edit_delete_file,
    "create_file": edit_create_file,
    "caller_of_caller": edit_caller_of_caller,
}


def _composite(seed: int) -> Edit:
    # A seeded sample of the single edits applied in sequence, so the
    # property holds for combinations an agent's turn actually produces and
    # not only for one edit at a time.
    def apply(root: Path) -> tuple[list[str], list[str]]:
        rng = random.Random(seed)
        names = rng.sample(sorted(SINGLE_EDITS), k=3)
        changed: dict[str, None] = {}
        deleted: dict[str, None] = {}
        for name in names:
            if name == "delete_file" and not (root / "pkg/unrelated.py").exists():
                continue
            c, d = SINGLE_EDITS[name](root)
            for rel in c:
                deleted.pop(rel, None)
                changed[rel] = None
            for rel in d:
                changed.pop(rel, None)
                deleted[rel] = None
        return list(changed), list(deleted)

    return apply


Snapshot = tuple[frozenset[tuple[str, str]], frozenset[tuple[str, ...]]]


def _snapshot(store: _StatefulIngestor) -> Snapshot:
    nodes = frozenset((label, str(uid)) for (label, uid) in store.nodes)
    edges = frozenset(
        (str(fl), str(fv), str(rel), str(tl), str(tv))
        + tuple(sorted(f"{k}={v}" for k, v in store.edge_props.get(e, {}).items()))
        for e in store.edges
        for (fl, fv, rel, tl, tv) in [e]
    )
    return nodes, edges


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


def _materialise(root: Path) -> None:
    for rel, text in FIXTURE.items():
        _write(root, rel, text)


@pytest.fixture
def fixture_root(temp_repo: Path) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    _materialise(root)
    return root


def _clean_index(root: Path) -> Snapshot:
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    return _snapshot(store)


def _diff(actual: Snapshot, expected: Snapshot) -> str:
    lines = []
    for kind, a, e in (
        ("node", actual[0], expected[0]),
        ("edge", actual[1], expected[1]),
    ):
        for item in sorted(a - e):
            lines.append(f"extra {kind}: {item}")
        for item in sorted(e - a):
            lines.append(f"missing {kind}: {item}")
    return "\n".join(lines)


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
@pytest.mark.parametrize(
    "edit",
    [*SINGLE_EDITS.values(), *(_composite(seed) for seed in range(4))],
    ids=[*SINGLE_EDITS, *(f"composite_{seed}" for seed in range(4))],
)
def test_reingest_matches_a_clean_index(
    fixture_root: Path, edit: Edit, fresh_updater: bool
) -> None:
    """Graph after reingest(edit) == graph after a full index of the edit.

    `warm` reuses the updater that built the graph (the watcher's shape);
    `fresh` builds a new one on the existing graph (the MCP tool's shape,
    which must read the registry back before it can resolve anything).
    """
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)

    changed, deleted = edit(fixture_root)
    if fresh_updater:
        updater = _updater(store, fixture_root)
    updater.reingest(changed, deleted=deleted)

    actual = _snapshot(store)
    expected = _clean_index(fixture_root)
    assert actual == expected, _diff(actual, expected)


SIBLING_JS = "export function helper() { return 1; }\n"
SIBLING_TS = "export function helper2(): number { return 2; }\n"
SIBLING_MAIN = (
    "import { helper } from './util';\n\nexport function go() { return helper(); }\n"
)


def _sibling_updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="sib",
    )


def _sibling_clean(root: Path) -> Snapshot:
    # The on-disk tree already holds the edited state; a fresh store on the
    # same root gives the graph a clean index of it.
    store = _StatefulIngestor()
    _sibling_updater(store, root).run(force=True)
    return _snapshot(store)


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
@pytest.mark.parametrize(
    ("before", "after", "changed", "deleted"),
    [
        (
            {"util.js": SIBLING_JS, "main.js": SIBLING_MAIN},
            {"util.js": SIBLING_JS, "util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            ["util.ts"],
            [],
        ),
        (
            {"util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            {"util.js": SIBLING_JS, "util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            ["util.js"],
            [],
        ),
        (
            {"util.js": SIBLING_JS, "util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            {"util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            [],
            ["util.js"],
        ),
        (
            {"util.js": SIBLING_JS, "util.ts": SIBLING_TS, "main.js": SIBLING_MAIN},
            {
                "util.js": SIBLING_JS,
                "util.ts": SIBLING_TS.replace("return 2", "return 3"),
                "main.js": SIBLING_MAIN,
            },
            ["util.ts"],
            [],
        ),
    ],
    ids=["add_loser", "add_winner", "delete_winner", "modify_loser"],
)
def test_reingest_gives_same_stem_siblings_their_clean_index_qns(
    temp_repo: Path,
    before: dict[str, str],
    after: dict[str, str],
    changed: list[str],
    deleted: list[str],
    fresh_updater: bool,
) -> None:
    # The first same-stem sibling in walk order owns the bare module qn
    # (issue #1569). Adding the sibling that wins that order, or deleting
    # the one that held it, changes the survivor's qn, so the scoped path
    # must re-parse the survivor unseeded, as the batch path does, and a
    # fresh updater must see the taken qns before it parses anything.
    root = temp_repo / "sib"
    root.mkdir()
    for rel, text in before.items():
        _write(root, rel, text)
    store = _StatefulIngestor()
    updater = _sibling_updater(store, root)
    updater.run(force=True)
    for rel in deleted:
        (root / rel).unlink()
    for rel in changed:
        _write(root, rel, after[rel])
    if fresh_updater:
        updater = _sibling_updater(store, root)

    report = updater.reingest(changed, deleted=deleted)

    actual = _snapshot(store)
    expected = _sibling_clean(root)
    assert actual == expected, _diff(actual, expected)
    # Every file the call touched is in the report: re-parsed survivors of
    # a stem in flux count as dependents.
    touched = {*report.reparsed, *report.affected, *report.removed}
    assert touched == set(updater._reparsed_file_keys)


def test_reingest_aborts_when_the_module_paths_cannot_be_read(
    temp_repo: Path,
) -> None:
    # A sink that claims a query surface but fails the module-path read
    # leaves the taken qns unknown; seeding nothing would let a re-parsed
    # loser sibling claim the winner's bare qn. The call aborts before any
    # delete, so the graph is untouched.
    root = temp_repo / "sib"
    root.mkdir()
    _write(root, "util.js", SIBLING_JS)
    _write(root, "util.ts", SIBLING_TS)
    _write(root, "main.js", SIBLING_MAIN)
    store = _StatefulIngestor()
    _sibling_updater(store, root).run(force=True)
    before = _snapshot(store)
    real_fetch_all = store.fetch_all

    def failing(query: str, params: dict | None = None) -> list:
        if query == cs.CYPHER_PROJECT_MODULE_PATHS:
            raise RuntimeError("graph down")
        return real_fetch_all(query, params)

    store.fetch_all = failing  # type: ignore[method-assign]
    _write(root, "util.ts", SIBLING_TS.replace("return 2", "return 3"))
    updater = _sibling_updater(store, root)

    with pytest.raises(ReingestAborted, match="module paths could not be read") as info:
        updater.reingest(["util.ts"])

    assert isinstance(info.value.__cause__, RuntimeError)
    assert _snapshot(store) == before


def test_a_fresh_updater_forgets_a_deleted_modules_rehydrated_qn(
    temp_repo: Path,
) -> None:
    # A fresh updater reads module qns back from the graph; the deleted
    # file's qn must leave `known_module_paths` with the rest of its state,
    # which keys on the map entry only the seed provides on this path.
    root = temp_repo / "sib"
    root.mkdir()
    _write(root, "util.py", "def helper():\n    return 1\n")
    _write(
        root, "main.py", "from util import helper\n\n\ndef go():\n    return helper()\n"
    )
    store = _StatefulIngestor()
    _sibling_updater(store, root).run(force=True)
    (root / "util.py").unlink()
    _write(root, "main.py", "def go():\n    return 1\n")
    updater = _sibling_updater(store, root)

    updater.reingest(["main.py"], deleted=["util.py"])

    assert "sib.util" not in updater.known_module_paths()
    assert "sib.main" in updater.known_module_paths()


def test_reingest_reparses_in_walk_order_across_directories(
    temp_repo: Path,
) -> None:
    # The bare module qn goes to the first same-stem sibling the walk
    # yields; a scoped call must re-parse in that order however its paths
    # were given, including across directories.
    root = temp_repo / "order"
    root.mkdir()
    layout = ["z.py", "a.py", "sub/b.py", "sub/a.py", "sub/deep/c.py", "b.py"]
    for rel in layout:
        _write(root, rel, "def f():\n    return 1\n")
    store = _StatefulIngestor()
    updater = _sibling_updater(store, root)
    updater.run(force=True)
    walk_order = [key for _fp, key in updater._collect_eligible_files()]
    seen: list[str] = []
    real_delete = updater._reingest_delete

    def spy(reparse: dict[str, Path], gone: dict[str, Path], hashes: dict) -> None:
        seen.extend(reparse)
        real_delete(reparse, gone, hashes)

    updater._reingest_delete = spy  # type: ignore[method-assign]
    shuffled = [layout[i] for i in (3, 0, 5, 2, 1, 4)]

    updater.reingest(shuffled)

    assert seen == [key for key in walk_order if key in set(layout)]
    assert seen != shuffled


def _warnings_during(action: Callable[[], object]) -> list[str]:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        action()
    finally:
        logger.remove(sink)
    return lines


def test_reingest_removes_the_cache_when_it_cannot_backdate_it(
    fixture_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cache stamped with the wrong instant hides edits from the next run,
    # so a failed backdate removes it and says so with its own message.
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    cache = fixture_root / cs.HASH_CACHE_FILENAME
    changed, deleted = edit_rename_callee(fixture_root)

    def failing_utime(path: object, times: object = None, **kwargs: object) -> None:
        raise OSError(1, "EPERM utime")

    monkeypatch.setattr(os, "utime", failing_utime)
    warnings = _warnings_during(lambda: updater.reingest(changed, deleted=deleted))

    assert not cache.exists()
    assert any(
        "Could not backdate" in line and "EPERM utime" in line for line in warnings
    )


def test_reingest_reports_a_cache_it_could_neither_backdate_nor_remove(
    fixture_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    cache = fixture_root / cs.HASH_CACHE_FILENAME
    changed, deleted = edit_rename_callee(fixture_root)

    def failing_utime(path: object, times: object = None, **kwargs: object) -> None:
        raise OSError(1, "EPERM utime")

    real_unlink = Path.unlink

    def failing_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == cache:
            raise OSError(1, "EPERM unlink")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(os, "utime", failing_utime)
    monkeypatch.setattr(Path, "unlink", failing_unlink)
    warnings = _warnings_during(lambda: updater.reingest(changed, deleted=deleted))

    assert cache.exists()
    assert any("Could not backdate" in line for line in warnings)
    assert any(
        "Could not remove" in line and "EPERM unlink" in line for line in warnings
    )


TYPED_FILES = {
    "types_.py": "class A:\n    pass\n\n\nclass B:\n    pass\n",
    "mod.py": "from types_ import A, B\n\n\ndef f(x: A) -> A:\n    return x\n",
}


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
def test_reingest_replaces_the_type_edges_of_a_changed_annotation(
    temp_repo: Path, fresh_updater: bool
) -> None:
    # A fresh updater reads the old annotation back from the graph before the
    # scoped delete; that requeued fact must go with the module, or the old
    # RETURNS/ACCEPTS edge is emitted beside the new one (issue #1527).
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in TYPED_FILES.items():
        _write(root, rel, text)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    _write(root, "mod.py", TYPED_FILES["mod.py"].replace("A", "B"))
    if fresh_updater:
        updater = _updater(store, root)

    updater.reingest(["mod.py"])

    actual = _snapshot(store)
    expected = _clean_index(root)
    assert actual == expected, _diff(actual, expected)
    typed = {
        (e[1], e[2], e[4])
        for e in actual[1]
        if e[2]
        in (cs.RelationshipType.RETURNS.value, cs.RelationshipType.ACCEPTS.value)
    }
    assert typed == {
        (f"{PROJECT}.mod.f", cs.RelationshipType.RETURNS.value, f"{PROJECT}.types_.B"),
        (f"{PROJECT}.mod.f", cs.RelationshipType.ACCEPTS.value, f"{PROJECT}.types_.B"),
    }


@pytest.mark.parametrize("fresh_updater", [False, True], ids=["warm", "fresh"])
def test_reingest_of_a_type_keeps_the_annotation_edges_of_a_non_importing_referrer(
    temp_repo: Path, fresh_updater: bool
) -> None:
    # An annotation resolves a unique project type by suffix without an
    # import, so the referring file is not a dependent of the type's file.
    # Re-ingesting the type recreates its node; the referrer's RETURNS and
    # ACCEPTS edges must be captured and restored like the other inbound
    # edges, or they vanish until the referrer is edited (issue #1527).
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "types_.py", "class Widget:\n    pass\n")
    _write(root, "mod.py", "def f(x: Widget) -> Widget:\n    return x\n")
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    _write(root, "types_.py", "class Widget:\n    pass\n\n\nclass Other:\n    pass\n")
    if fresh_updater:
        updater = _updater(store, root)

    report = updater.reingest(["types_.py"])

    assert report.affected == (), "mod.py must not need a re-parse to keep its edges"
    actual = _snapshot(store)
    expected = _clean_index(root)
    assert actual == expected, _diff(actual, expected)
    typed = {
        (e[1], e[2], e[4])
        for e in actual[1]
        if e[2]
        in (cs.RelationshipType.RETURNS.value, cs.RelationshipType.ACCEPTS.value)
    }
    assert typed == {
        (
            f"{PROJECT}.mod.f",
            cs.RelationshipType.RETURNS.value,
            f"{PROJECT}.types_.Widget",
        ),
        (
            f"{PROJECT}.mod.f",
            cs.RelationshipType.ACCEPTS.value,
            f"{PROJECT}.types_.Widget",
        ),
    }


def test_reingest_reports_dependents_and_removals(fixture_root: Path) -> None:
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)

    changed, deleted = edit_rename_callee(fixture_root)
    (fixture_root / "pkg/unrelated.py").unlink()
    report = updater.reingest(changed, deleted=["pkg/unrelated.py"])

    assert report.reparsed == ("pkg/util.py",)
    # app.py imports util.py, so it re-parses with it; main.py is two levels
    # away and keeps its edges untouched.
    assert report.affected == ("pkg/app.py",)
    assert report.removed == ("pkg/unrelated.py",)
    assert report.elapsed_ms > 0


def test_reingest_absent_path_counts_as_removed(fixture_root: Path) -> None:
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    (fixture_root / "pkg/unrelated.py").unlink()

    report = updater.reingest(["pkg/unrelated.py"])

    assert report.removed == ("pkg/unrelated.py",)
    assert (cs.NodeLabel.MODULE.value, f"{PROJECT}.pkg.unrelated") not in store.nodes


def test_reingest_refuses_paths_outside_the_repo(fixture_root: Path) -> None:
    updater = _updater(_StatefulIngestor(), fixture_root)
    with pytest.raises(ValueError, match="outside the repository"):
        updater.reingest(["../escape.py"])
    with pytest.raises(ValueError, match="outside the repository"):
        updater.reingest([], deleted=[fixture_root.parent / "other.py"])


def test_reingest_refuses_a_directory(fixture_root: Path) -> None:
    # A directory sent as a path lands in `gone` (it is not a file) and the
    # delete queries match nothing, so without the refusal the report says
    # the package was removed while the graph is untouched.
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    before = _snapshot(store)
    with pytest.raises(ValueError, match="a directory"):
        updater.reingest(["pkg"])
    assert _snapshot(store) == before


def test_reingest_deletes_a_file_a_directory_has_replaced(fixture_root: Path) -> None:
    # The watcher's DELETE event can arrive after a directory of the same
    # name has been created; the deletion is an instruction, so the stale
    # Module still goes even though the name is now a directory.
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    util = fixture_root / "pkg" / "util.py"
    util.unlink()
    util.mkdir()

    report = updater.reingest([], deleted=["pkg/util.py"])

    assert report.removed == ("pkg/util.py",)
    assert (cs.NodeLabel.MODULE.value, f"{PROJECT}.pkg.util") not in store.nodes


def test_reingest_with_nothing_to_do_is_a_no_op(fixture_root: Path) -> None:
    store = MagicMock()
    report = _updater(store, fixture_root).reingest([])
    assert report == (tuple(), tuple(), tuple(), tuple(), 0.0)
    store.flush_all.assert_not_called()


def test_reingest_keeps_the_hash_cache_current(fixture_root: Path) -> None:
    """A later update_repository must not re-parse what reingest applied."""
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    cache = fixture_root / cs.HASH_CACHE_FILENAME
    assert cache.is_file()

    changed, deleted = edit_add_function_and_call(fixture_root)
    (fixture_root / "pkg/unrelated.py").unlink()
    updater.reingest(changed, deleted=["pkg/unrelated.py"])

    hashes = json.loads(cache.read_text(encoding="utf-8"))
    for rel in changed:
        digest = hashlib.md5(
            (fixture_root / rel).read_bytes(), usedforsecurity=False
        ).hexdigest()
        assert hashes[rel] == digest, rel
    assert "pkg/unrelated.py" not in hashes


@pytest.mark.parametrize(
    ("filename", "expected", "not_expected"),
    [
        (
            "svc.go",
            [
                "_run_go_frontend",
                "_rehydrate_go_type_locations",
                "_rehydrate_function_locations",
                "_join_go_implements",
            ],
            ["_run_csharp_frontend", "_run_java_frontend"],
        ),
        (
            "Svc.cs",
            [
                "_run_csharp_frontend",
                "_rehydrate_csharp_type_locations",
                "_join_csharp_partials",
            ],
            ["_run_go_frontend"],
        ),
        (
            "Svc.java",
            ["_run_java_frontend", "_rehydrate_function_locations"],
            ["_run_go_frontend"],
        ),
        (
            "app.py",
            ["_run_python_frontend"],
            ["_run_go_frontend", "_run_csharp_frontend", "_run_java_frontend"],
        ),
    ],
)
def test_reingest_reruns_the_language_frontend(
    temp_repo: Path,
    mock_ingestor: MagicMock,
    filename: str,
    expected: list[str],
    not_expected: list[str],
) -> None:
    """Semantic facts are keyed against the compiler's view of the module.

    A change in one file can rebind calls in unchanged files (issue #1229
    phase 3), so the applicable frontend re-runs on the reingest path exactly
    as it did on the watcher's own path before the two were unified.
    """
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    names = [
        "_run_go_frontend",
        "_rehydrate_go_type_locations",
        "_rehydrate_function_locations",
        "_join_go_implements",
        "_run_csharp_frontend",
        "_rehydrate_csharp_type_locations",
        "_join_csharp_partials",
        "_run_java_frontend",
        "_run_python_frontend",
    ]
    mocks = {name: MagicMock() for name in names}
    for name, mock in mocks.items():
        setattr(updater, name, mock)
    path = temp_repo / filename
    path.write_text("// change\n", encoding="utf-8")

    updater.reingest([path])

    for name in expected:
        assert mocks[name].called, name
    for name in not_expected:
        assert not mocks[name].called, name


def test_reingest_deletion_also_reruns_the_frontend(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Removing a file changes the module's bindings just as an edit does.
    gone = temp_repo / "gone.go"
    gone.write_text("package x\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    gone.unlink()
    run_go = MagicMock()
    updater._run_go_frontend = run_go  # type: ignore[method-assign]

    updater.reingest([], deleted=[gone])

    run_go.assert_called_once()


def test_full_and_scoped_call_pass_agree_on_a_subset(fixture_root: Path) -> None:
    """_process_function_calls(only=...) emits exactly the subset's edges."""
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    before = {e for e in store.edges if e[2] == cs.RelationshipType.CALLS.value}

    store.reset_edges()
    updater.factory.call_processor.reset_resolution_caches()
    updater._process_function_calls(only={fixture_root / "pkg/app.py"})
    scoped = {e for e in store.edges if e[2] == cs.RelationshipType.CALLS.value}

    assert scoped == {e for e in before if e[1] == f"{PROJECT}.pkg.app.run"}
    assert scoped, "the subset file has a call edge to compare"


# --- What the watcher used to do inline, now reingest's contract ---------------


def _writes(mock: MagicMock, query: str) -> list[dict]:
    return [
        c.args[1] if len(c.args) > 1 else c.kwargs.get("parameters", {})
        for c in mock.execute_write.call_args_list
        if c.args[0] == query
    ]


def _file_nodes(mock: MagicMock, path: Path) -> int:
    return sum(
        1
        for c in mock.ensure_node_batch.call_args_list
        if str(c.args[0]) == cs.NodeLabel.FILE.value
        and c.args[1].get(cs.KEY_NAME) == path.name
    )


def test_reingest_deletes_the_module_project_scoped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """The Module delete carries the project scope, or a sibling project
    sharing the relative path in the same graph loses its module."""
    (temp_repo / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    mock_ingestor.reset_mock()

    updater.reingest(["mod.py"])

    assert _writes(mock_ingestor, cs.CYPHER_DELETE_MODULE) == [
        {
            cs.KEY_PATH: "mod.py",
            cs.KEY_PROJECT_NAME: updater.project_name,
            cs.KEY_PROJECT_PREFIX: f"{updater.project_name}.",
        }
    ]
    # A live file's File node is re-merged by the re-parse, not deleted.
    assert _writes(mock_ingestor, cs.CYPHER_DELETE_FILE) == []


def test_reingest_removes_a_deleted_file_by_absolute_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    """A File node is keyed on the absolute path (issue #897)."""
    gone = temp_repo / "notes.md"
    gone.write_text("# notes\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    gone.unlink()
    mock_ingestor.reset_mock()

    updater.reingest([], deleted=[gone])

    assert _writes(mock_ingestor, cs.CYPHER_DELETE_FILE) == [
        {cs.KEY_PATH: gone.resolve().as_posix()}
    ]
    assert len(_writes(mock_ingestor, cs.CYPHER_DELETE_MODULE)) == 1


@pytest.mark.parametrize("filename", ["plan.md", "app.rb"])
def test_reingest_reparses_secondary_tier_files(
    temp_repo: Path, mock_ingestor: MagicMock, filename: str
) -> None:
    """The delete comes first, so a tier that does not re-parse EMPTIES the
    file in the graph rather than refreshing it (issue #1427)."""
    path = temp_repo / filename
    path.write_text("# Heading\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    secondary = MagicMock(return_value=True)
    updater.process_with_secondary_tier = secondary  # type: ignore[method-assign]

    updater.reingest([path])

    secondary.assert_called_once_with(path)


def test_reingest_parses_a_tree_sitter_file_once(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    path = temp_repo / "mod.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    secondary = MagicMock(return_value=True)
    updater.process_with_secondary_tier = secondary  # type: ignore[method-assign]
    mock_ingestor.reset_mock()

    updater.reingest([path])

    secondary.assert_not_called()
    assert _file_nodes(mock_ingestor, path) == 1


def test_reingest_creates_file_nodes_for_non_code_files(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    path = temp_repo / "config.json"
    path.write_text("{}", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    mock_ingestor.reset_mock()

    updater.reingest([path])

    assert _file_nodes(mock_ingestor, path) == 1


# --- Deferred definition-level relationships (Greptile P1 on #1538) -----------


CPP_FIXTURE: dict[str, str] = {
    "base.h": "class Base {\n public:\n  virtual int run();\n};\n",
    "derived.h": (
        '#include "base.h"\n\nclass Derived : public Base {\n public:\n'
        "  int run() override;\n};\n"
    ),
    "derived.cpp": '#include "derived.h"\n\nint Derived::run() { return 1; }\n',
}


@pytest.mark.parametrize("edited", ["derived.h", "derived.cpp", "base.h"])
def test_cpp_reingest_keeps_deferred_relationships(
    temp_repo: Path, edited: str
) -> None:
    """INHERITS, includes and out-of-class method containment are resolved
    in deferred stages after Pass 2; a scoped re-ingest that skipped them
    left the re-parsed file without them until a full update."""
    root = temp_repo / "cpp_reingest"
    root.mkdir()
    for rel, text in CPP_FIXTURE.items():
        _write(root, rel, text)
    parsers, _queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    before = _snapshot(store)
    present = {e[2] for e in before[1]}
    for rel in (
        cs.RelationshipType.INHERITS,
        cs.RelationshipType.IMPORTS,
        cs.RelationshipType.DEFINES_METHOD,
        cs.RelationshipType.OVERRIDES,
    ):
        assert rel.value in present, f"fixture must produce a {rel.value} edge"

    path = root / edited
    path.write_text(path.read_text() + "// touched\n", encoding="utf-8")
    updater.reingest([path])
    actual = _snapshot(store)

    # The relationships the deferred stages produce must all be back. Full
    # snapshot equality is not the oracle here: the batch incremental path
    # itself re-registers an out-of-class C++ method under a module-anchored
    # qn beside its class-anchored one, in a parse-order-dependent way
    # (pre-existing, filed separately), so only edges between nodes the
    # clean index knows are compared.
    deferred_rels = {
        cs.RelationshipType.INHERITS.value,
        cs.RelationshipType.IMPORTS.value,
        cs.RelationshipType.DEFINES_METHOD.value,
        cs.RelationshipType.OVERRIDES.value,
    }
    known = before[0]

    def deferred(snapshot: Snapshot) -> frozenset[tuple[str, ...]]:
        return frozenset(
            e
            for e in snapshot[1]
            if e[2] in deferred_rels and (e[0], e[1]) in known and (e[3], e[4]) in known
        )

    assert deferred(actual) == deferred(before), _diff(
        (frozenset(), deferred(actual)), (frozenset(), deferred(before))
    )
    assert known <= actual[0], sorted(known - actual[0])


def test_reingest_reruns_the_libclang_frontend_for_c_family_files(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In LIBCLANG mode the frontend emits C/C++ definitions itself and marks
    covered files for the tree-sitter pass to skip; a scoped re-ingest that
    deleted the subtree must run it again before re-parsing."""
    from codebase_rag.config import settings

    path = temp_repo / "a.cpp"
    path.write_text("int f() { return 1; }\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    order: list[str] = []
    updater._run_cpp_frontend = MagicMock(side_effect=lambda: order.append("frontend"))  # type: ignore[method-assign]
    original = updater._process_single_file

    def spy(*args: object, **kwargs: object) -> None:
        order.append("parse")
        original(*args, **kwargs)  # type: ignore[arg-type]

    updater._process_single_file = spy  # type: ignore[method-assign]

    updater.reingest([path])

    assert order[:2] == ["frontend", "parse"]


def test_reingest_of_a_python_file_does_not_run_the_cpp_frontend(
    temp_repo: Path, mock_ingestor: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag.config import settings

    path = temp_repo / "a.py"
    path.write_text("x = 1\n", encoding="utf-8")
    updater = create_and_run_updater(temp_repo, mock_ingestor)
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.LIBCLANG)
    run_cpp = MagicMock()
    updater._run_cpp_frontend = run_cpp  # type: ignore[method-assign]
    updater.reingest([path])
    run_cpp.assert_not_called()


def test_reingest_does_not_hide_an_edit_it_was_not_told_about(
    fixture_root: Path,
) -> None:
    """The cache's mtime must not move past edits reingest never saw.

    An agent edits two files and reports one. The hash cache written by the
    reingest must not be stamped later than the other edit, or the next
    update_repository reads that file as no newer than the cache and keeps
    its stale hash for good.
    """
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    cache = fixture_root / cs.HASH_CACHE_FILENAME
    stamped_before = cache.stat().st_mtime

    # Both edits land after the cache was stamped; only one is reported.
    _write(
        fixture_root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"] + "\n\ndef fresh():\n    return 4\n",
    )
    _write(
        fixture_root,
        "pkg/unrelated.py",
        FIXTURE["pkg/unrelated.py"] + "\n\ndef newly_added():\n    return 5\n",
    )
    # A coarse filesystem clock could stamp the edits no later than the cache;
    # the incremental skip compares mtimes, so both are pushed past it.
    for rel in ("pkg/util.py", "pkg/unrelated.py"):
        os.utime(fixture_root / rel, (stamped_before + 1, stamped_before + 1))
    updater.reingest(["pkg/util.py"])
    assert cache.stat().st_mtime == stamped_before, "reingest moved the cache mtime"

    # The next update, on a fresh updater over the same graph, re-parses the
    # unreported file (its new definition lands) and nothing else: the
    # reported file's hash is already current. The graph then equals a
    # clean index.
    later_updater = _updater(store, fixture_root)
    parsed: list[str] = []
    real_parse = later_updater._process_single_file

    def spy(path: Path, *args: object, **kwargs: object) -> None:
        parsed.append(path.relative_to(fixture_root).as_posix())
        real_parse(path, *args, **kwargs)

    later_updater._process_single_file = spy  # type: ignore[method-assign]
    later_updater.run(force=False)
    assert parsed == ["pkg/unrelated.py"]
    assert _diff(_snapshot(store), _clean_index(fixture_root)) == ""


@pytest.mark.parametrize(
    ("rel", "exclude"),
    [
        ("node_modules/leak.py", None),
        ("pkg/bundle.min.js", None),
        ("vendor/v.py", frozenset({"vendor"})),
    ],
    ids=["ignored_dir", "ignored_suffix", "cli_exclude"],
)
@pytest.mark.parametrize("via_deleted", [False, True], ids=["paths", "deleted"])
def test_reingest_skips_paths_the_ignore_rules_exclude(
    fixture_root: Path, rel: str, exclude: frozenset[str] | None, via_deleted: bool
) -> None:
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=fixture_root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        exclude_paths=exclude,
    )
    updater.run(force=True)
    before = _snapshot(store)
    _write(fixture_root, rel, "def leaked():\n    return 1\n")

    # The `deleted` list runs the same ignore check: an excluded path named
    # as deleted is reported skipped, not removed, and issues no delete.
    report = (
        updater.reingest([], deleted=[rel]) if via_deleted else updater.reingest([rel])
    )

    assert report.skipped == (rel,)
    assert report.reparsed == ()
    assert report.affected == ()
    assert report.removed == ()
    assert _snapshot(store) == before
    cache = json.loads((fixture_root / cs.HASH_CACHE_FILENAME).read_text())
    assert rel not in cache


def test_a_warm_updater_aborts_when_the_inbound_capture_fails(
    fixture_root: Path,
) -> None:
    # reingest must not inherit the previous run's full-build flag: with it
    # set, a failed inbound-edge capture is swallowed and the cross-file
    # edges into the re-parsed files are dropped under a success log.
    store = _StatefulIngestor()
    updater = _updater(store, fixture_root)
    updater.run(force=True)
    _write(
        fixture_root, "pkg/util.py", FIXTURE["pkg/util.py"] + "\n\ndef f():\n    pass\n"
    )
    real_fetch_all = store.fetch_all

    def failing(query: str, params: object = None) -> list:
        if query == cs.CYPHER_INBOUND_EDGES:
            raise RuntimeError("inbound-edge query outage")
        return real_fetch_all(query, params)  # type: ignore[arg-type]

    store.fetch_all = failing  # type: ignore[method-assign]
    before = _snapshot(store)
    with pytest.raises(ReingestAborted, match="inbound-edge query outage"):
        updater.reingest(["pkg/util.py"])
    assert _snapshot(store) == before


ROUTES_PY = (
    "from fastapi import APIRouter\n\nrouter = APIRouter(prefix='/users')\n\n\n"
    "@router.get('/{user_id}')\ndef get_user(user_id: int):\n    return {}\n"
)
MAIN_PY = (
    "from fastapi import FastAPI\n\nimport routes\n\napp = FastAPI()\n"
    "app.include_router(routes.router, prefix='{prefix}')\n"
)


class _QueryableSink:
    """Answers the graph read-backs the endpoint pass makes; nothing else."""

    def __init__(
        self,
        project: str,
        modules: Mapping[str, str] | None = None,
        handlers: tuple[str, ...] = ("routes.get_user",),
    ) -> None:
        self.project = project
        # Full module qn -> repo-relative path, as the graph would answer.
        self.modules = dict(
            modules or {f"{project}.routes": "routes.py", f"{project}.main": "main.py"}
        )
        self.handlers = handlers
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()
        self.flush_all = MagicMock()
        self.execute_write = MagicMock()
        self.fetch_all = MagicMock(side_effect=self._fetch)

    def _fetch(self, query: str, params: object = None) -> list:
        if query in (CYPHER_PROJECT_MODULES, CYPHER_PROJECT_PY_MODULES):
            return [
                {"qualified_name": qn, "path": path}
                for qn, path in self.modules.items()
            ]
        if query == CYPHER_PROJECT_ROUTE_HANDLERS:
            return [
                {
                    "labels": ["Function"],
                    "qualified_name": f"{self.project}.{handler}",
                    "decorators": ["@router.get('/{user_id}')"],
                }
                for handler in self.handlers
            ]
        return []

    def stale_delete_qns(self) -> set[str]:
        return {
            qn.split(".", 1)[1]
            for c in self.execute_write.call_args_list
            if c.args and "EXPOSES" in c.args[0]
            for qn in c.args[1]["qns"]
        }

    def exposes(self) -> set[tuple[str, str]]:
        rel = cs.RelationshipType.EXPOSES.value
        return {
            (c.args[0][2].split(".", 1)[1], c.args[2][2].split("::")[-1])
            for c in self.ensure_relationship_batch.call_args_list
            if str(c.args[1]) == rel
        }


def test_reingest_re_emits_handlers_under_an_edited_mount_prefix(
    temp_repo: Path,
) -> None:
    # A mount-only edit re-parses main.py alone; the unchanged handler in
    # routes.py must still come back from the graph and re-emit under the
    # new prefix, as the batch incremental run does.
    root = temp_repo / "mounts"
    root.mkdir()
    _write(root, "routes.py", ROUTES_PY)
    _write(root, "main.py", MAIN_PY.format(prefix="/api"))
    sink = _QueryableSink("mounts")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=sink,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
        project_name="mounts",
    )
    updater.run()
    assert sink.exposes() == {("routes.get_user", "GET /api/users/{user_id}")}
    sink.ensure_relationship_batch.reset_mock()
    sink.execute_write.reset_mock()
    _write(root, "main.py", MAIN_PY.format(prefix="/v2"))

    updater.reingest(["main.py"])

    assert sink.exposes() == {("routes.get_user", "GET /v2/users/{user_id}")}
    stale_deletes = [
        c.args[0]
        for c in sink.execute_write.call_args_list
        if c.args and "EXPOSES" in c.args[0]
    ]
    assert stale_deletes, "the stale EXPOSES delete was not issued"


def _mount_updater(root: Path, sink: _QueryableSink) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=sink,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
        project_name=sink.project,
    )


def test_a_mount_edit_under_a_package_root_leaves_unrelated_routers_alone(
    temp_repo: Path,
) -> None:
    # A package root (`__init__.py`) makes the import processor record a
    # sibling `import routes` bare, and `from . import helper` pulls the
    # package module itself into the scoped set. The scoped pass must still
    # find routes.py through the registry's own import resolution, and must
    # not let the package module stand in for other.py: app2's handler is
    # neither deleted nor re-emitted by an edit to main.py.
    root = temp_repo / "rootp"
    root.mkdir()
    _write(root, "__init__.py", "def helper():\n    return 1\n")
    _write(root, "routes.py", ROUTES_PY)
    _write(root, "other.py", ROUTES_PY.replace("/users", "/things"))
    main = (
        "from fastapi import FastAPI\n\nfrom . import helper\nimport routes\n\n"
        "app = FastAPI()\napp.include_router(routes.router, prefix='{prefix}')\n"
    )
    _write(root, "main.py", main.format(prefix="/api"))
    _write(
        root,
        "app2.py",
        "from fastapi import FastAPI\n\nimport other\n\napp = FastAPI()\n"
        "app.include_router(other.router, prefix='/second')\n",
    )
    sink = _QueryableSink("rootp", handlers=("routes.get_user", "other.get_user"))
    updater = _mount_updater(root, sink)
    updater.run()
    assert sink.exposes() == {
        ("routes.get_user", "GET /api/users/{user_id}"),
        ("other.get_user", "GET /second/things/{user_id}"),
    }
    dp = updater.factory.definition_processor
    sink.modules = {
        qn: path.relative_to(root).as_posix()
        for qn, path in dp.module_qn_to_file_path.items()
    }
    assert set(sink.modules) >= {
        "rootp",
        "rootp.routes",
        "rootp.other",
        "rootp.main",
        "rootp.app2",
    }
    sink.ensure_relationship_batch.reset_mock()
    sink.execute_write.reset_mock()
    _write(root, "main.py", main.format(prefix="/v2"))

    updater.reingest(["main.py"])

    assert sink.exposes() == {("routes.get_user", "GET /v2/users/{user_id}")}
    assert sink.stale_delete_qns() == {"routes.get_user"}


def test_a_mount_edit_rehydrates_a_nested_router_chain(temp_repo: Path) -> None:
    # main mounts api.api_router, which mounts routes.router: an edit to
    # main's prefix must pull in both levels, as the full build composes
    # them, or the handler two imports away keeps its stale prefix.
    root = temp_repo / "nest"
    root.mkdir()
    _write(root, "routes.py", ROUTES_PY)
    _write(
        root,
        "api.py",
        "from fastapi import APIRouter\n\nimport routes\n\napi_router = APIRouter()\n"
        "api_router.include_router(routes.router)\n",
    )
    main = (
        "from fastapi import FastAPI\n\nfrom api import api_router\n\n"
        "app = FastAPI()\napp.include_router(api_router, prefix='{prefix}')\n"
    )
    _write(root, "main.py", main.format(prefix="/api/v1"))
    sink = _QueryableSink(
        "nest",
        modules={
            "nest.routes": "routes.py",
            "nest.api": "api.py",
            "nest.main": "main.py",
        },
    )
    updater = _mount_updater(root, sink)
    updater.run()
    assert sink.exposes() == {("routes.get_user", "GET /api/v1/users/{user_id}")}
    sink.ensure_relationship_batch.reset_mock()
    sink.execute_write.reset_mock()
    _write(root, "main.py", main.format(prefix="/api/v2"))

    updater.reingest(["main.py"])

    assert sink.exposes() == {("routes.get_user", "GET /api/v2/users/{user_id}")}
    assert sink.stale_delete_qns() == {"routes.get_user"}
