# move (issue #1534) review findings from the stacked PRs #2070-#2080: each
# test names the failure it pins. The common shape is a move that used to
# report success -- or commit -- while leaving a tree that no longer runs,
# or that undid somebody else's edit on its way out.

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing import MoveRefused, move
from codebase_rag.editing.contract import Verdict
from codebase_rag.editing.transaction import EditTransaction, load_history
from codebase_rag.tests.test_move_op import (
    FIXTURE,
    PROJECT,
    _index,
    _materialise,
    _qn,
    _write,
)

# `codebase_rag.editing.move` the attribute is the op function; the module
# is what the monkeypatches below reach into.
move_mod = importlib.import_module("codebase_rag.editing.move")
# `pkg/b.py` imports `other` too, so every util variant keeps it defined.
OTHER = "\n\ndef other():\n    return 'o'\n"


def _move(root: Path, fixture_store, updater, target: str = "pkg.core", **kw):
    return move(
        root,
        fixture_store.fetch_all,
        PROJECT,
        kw.pop("qn", _qn("pkg.util.helper")),
        target,
        reingest=updater.reingest,
        **kw,
    )


def _python(root: Path, code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        check=False,
        capture_output=True,
        encoding=cs.ENCODING_UTF8,
    )


def _forced_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the postcondition fail whatever the delta says."""
    monkeypatch.setattr(
        move_mod, "verify", lambda *_a, **_k: Verdict(False, ("forced",), (), None)
    )


# --- rollback ---------------------------------------------------------------------


def test_failed_postcondition_never_undoes_a_later_unrelated_edit(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit lock is released before the re-ingest, so another edit can
    land between the move and its contract check. Rolling back "the newest
    transaction" then reverted THAT edit and left the failed move applied."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    _forced_failure(monkeypatch)
    calls = []

    def reingest(paths: list[str]) -> None:
        updater.reingest(paths)
        if not calls:
            other = EditTransaction(root)
            other.stage("notes.txt", "someone else's edit\n")
            assert other.commit().applied
        calls.append(paths)

    report = move(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        "pkg.core",
        reingest=reingest,
    )
    # The unrelated edit survives, and the report does not claim a rollback
    # that did not happen: the move is still on disk.
    assert (root / "notes.txt").read_text() == "someone else's edit\n"
    assert (root / "pkg/core.py").exists()
    assert report.applied is True
    assert "not rolled back" in report.message
    assert len(load_history(root)) == 2


def test_failed_postcondition_undoes_the_move_by_its_own_id(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    _forced_failure(monkeypatch)
    report = _move(root, store, updater)
    assert report.applied is False
    assert report.message == cs.MOVE_CONTRACT_FAILED.format(reasons="forced")
    for rel, text in FIXTURE.items():
        assert (root / rel).read_text() == text
    assert not (root / "pkg/core.py").exists()
    assert load_history(root) == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "the move now declares the pair, but structural_delta's lone-container "
        "pass only pairs a declared rename within one file"
    ),
)
def test_moving_an_empty_class_declares_the_rename_to_the_contract(
    temp_repo: Path,
) -> None:
    """An empty class has no fingerprint and no members, so the delta cannot
    infer that `util.Empty` and `core.Empty` are one definition; the move
    knows and must say so, or the contract sees a removal plus an addition."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = FIXTURE["pkg/util.py"] + "\n\nclass Empty:\n    pass\n"
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater, qn=_qn("pkg.util.Empty"))
    assert report.applied, report.message
    assert report.verdict is not None and report.verdict.ok


# --- the new destination is checked like every other file -------------------------


def test_a_new_destination_that_does_not_parse_is_not_committed(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    monkeypatch.setattr(
        move_mod.Mover, "_paste_text", lambda *_a, **_k: "def broken(:\n"
    )
    report = _move(root, store, updater)
    assert report.applied is False
    assert report.message == cs.MOVE_PARSE_FAILED.format(files="pkg/core.py")
    assert not (root / "pkg/core.py").exists()
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]


def test_a_target_in_another_language_is_refused(temp_repo: Path) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="language"):
        _move(root, store, updater, target="pkg/core.ts", dry_run=True)


def test_a_language_move_does_not_implement_is_refused(temp_repo: Path) -> None:
    """A loaded grammar is not an implementation: import generation writes
    Python syntax for anything that is not JavaScript or TypeScript."""
    root = temp_repo / PROJECT
    _write(root, "go.mod", "module example.com/m\n\ngo 1.21\n")
    _write(root, "util/util.go", "package util\n\nfunc Helper() int {\n\treturn 1\n}\n")
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="does not support"):
        _move(
            root,
            store,
            updater,
            qn=_qn("util.util.Helper"),
            target="core/core.go",
            dry_run=True,
        )


def test_a_target_outside_the_repository_is_refused(temp_repo: Path) -> None:
    """Only a missing file is a new destination; any other patcher refusal
    used to be read as "missing" and planned as a file outside the repo."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="outside the repository"):
        _move(root, store, updater, target="../outside.py", dry_run=True)


# --- what the definition takes with it --------------------------------------------


def test_a_nested_definition_is_refused(temp_repo: Path) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def outer():\n    def inner():\n        return 1\n\n    return inner()\n"
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="nested"):
        _move(root, store, updater, qn=_qn("pkg.util.outer.inner"))
    assert not (root / "pkg/core.py").exists()


def test_a_destination_that_already_binds_the_name_is_refused(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/core.py"] = "def helper():\n    return 'mine'\n"
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="already binds helper"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == fixture["pkg/core.py"]


def test_a_module_constant_the_definition_reads_is_imported(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "SEP = '-'\n\n\ndef helper(a):\n    return SEP.join(a)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert "from pkg.util import SEP" in (root / "pkg/core.py").read_text()
    probe = _python(root, "from pkg.core import helper; print(helper(['a', 'b']))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "a-b"


def test_future_directives_travel_with_the_definition(temp_repo: Path) -> None:
    """`annotations` is never referenced by name, so the use filter dropped
    it and the destination evaluated the annotation eagerly -- `NameError`
    at import time for a forward reference."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "from __future__ import annotations\n\n\n"
        "def helper(a: Later) -> Later:\n    return a\n\n\n"
        "class Later:\n    pass\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater, qn=_qn("pkg.util.helper"))
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert core.startswith("from __future__ import annotations\n")
    probe = _python(root, "import pkg.core")
    assert probe.returncode == 0, probe.stderr


def test_future_directives_are_added_at_the_head_of_an_existing_destination(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "from __future__ import annotations\n\n\n"
        "def helper(a: Later) -> Later:\n    return a\n\n\n"
        "class Later:\n    pass\n" + OTHER
    )
    fixture["pkg/core.py"] = '"""Core."""\n\nVERSION = 1\n'
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    core = (root / "pkg/core.py").read_text()
    assert core.startswith('"""Core."""\nfrom __future__ import annotations\n')
    assert ast.get_docstring(ast.parse(core)) == "Core."
    probe = _python(root, "import pkg.core")
    assert probe.returncode == 0, probe.stderr


def test_an_import_added_to_a_module_without_imports_keeps_its_docstring(
    temp_repo: Path,
) -> None:
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        '"""Utilities."""\n\n\n'
        "def helper(a):\n    return a\n\n\n"
        "def run():\n    return helper(1)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    util = (root / "pkg/util.py").read_text()
    assert ast.get_docstring(ast.parse(util)) == "Utilities."
    assert "from pkg.core import helper\n" in util


# --- dry run ----------------------------------------------------------------------


def test_dry_run_reports_the_diff_it_would_write(temp_repo: Path) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    report = _move(root, store, updater, dry_run=True)
    assert not report.applied
    assert "+def helper(a):" in report.diff
    assert "-def helper(a):" in report.diff
    assert "+from pkg.core import helper" in report.diff
    assert not (root / "pkg/core.py").exists()
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert load_history(root) == []


# --- cycle refusal ----------------------------------------------------------------


def test_an_importer_of_an_unrelated_name_does_not_block_the_move(
    temp_repo: Path,
) -> None:
    """`d` imports `other` from util, and `core` imports `d`. The move only
    rewires importers of `helper`; `d` gains no edge to `core`."""
    fixture = dict(FIXTURE)
    fixture["pkg/d.py"] = (
        "from pkg.util import other\n\n\ndef dd():\n    return other()\n"
    )
    fixture["pkg/core.py"] = "from pkg.d import dd\n\nVALUE = dd\n"
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message


def test_a_cycle_through_a_rewired_importer_outside_both_modules_is_refused(
    temp_repo: Path,
) -> None:
    """`c` imports `helper` and `core` imports `c`: rewiring `c` to `core`
    closes `c -> core -> c`. The simulated graph is project-wide, so a
    module outside the old and new paths still takes part."""
    fixture = dict(FIXTURE)
    fixture["pkg/c.py"] = (
        "from pkg.util import helper\n\n\ndef cc():\n    return helper([])\n"
    )
    fixture["pkg/core.py"] = "from pkg.c import cc\n\nVALUE = cc\n"
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused) as excinfo:
        _move(root, store, updater)
    assert excinfo.value.cycle == (_qn("pkg.c"), _qn("pkg.core"))


class _Ordered(list):
    """A cycle set whose iteration order the test controls."""

    def __sub__(self, other: object) -> list[frozenset[str]]:
        return [c for c in self if c not in other]  # type: ignore[operator]


def test_the_reported_cycle_is_chosen_deterministically(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    core, b, a = _qn("pkg.core"), _qn("pkg.b"), _qn("pkg.a")
    cycles = _Ordered([frozenset({b, core}), frozenset({a, core})])
    results = iter([_Ordered(), cycles])
    monkeypatch.setattr(move_mod, "import_cycles", lambda _graph: next(results))
    with pytest.raises(MoveRefused) as excinfo:
        _move(root, store, updater, dry_run=True)
    assert excinfo.value.cycle == (a, core)


# --- importer retargeting ---------------------------------------------------------


def test_an_importer_row_without_an_end_column_is_left_unchanged(
    temp_repo: Path,
) -> None:
    """A missing end column defaults to 0, which for an import not at column
    0 (here one inside a function body) makes a reversed span. The rewriter
    reads an empty statement there, matches nothing and leaves the file as
    it was; this pins that it keeps doing so rather than rewriting bytes."""
    fixture = dict(FIXTURE)
    fixture["pkg/a.py"] = (
        "def run():\n    from pkg.util import helper\n\n    return helper(['x', 'y'])\n"
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)

    def fetch_all(query: str, params=None):
        rows = store.fetch_all(query, params)
        if query == move_mod.cq.CYPHER_GRAPH_IMPORTERS:
            return [{**row, cs.KEY_END_COL: None} for row in rows]
        return rows

    report = move(
        root, fetch_all, PROJECT, _qn("pkg.util.helper"), "pkg.core", dry_run=True
    )
    assert report.importers == ()
    assert set(report.unchanged_importers) == {"pkg/a.py:2", "pkg/b.py:1"}


# --- JavaScript: what the moved code imports back must be exported ---------------

JS_UTIL = "export function helper(a) {\n  return a.join(SEP);\n}\n"


@pytest.mark.parametrize(
    ("declaration", "exported"),
    [
        pytest.param("export const SEP = '-';\n\n", True, id="exported"),
        pytest.param("const SEP = '-';\n\n", False, id="module-private"),
    ],
)
def test_a_js_module_constant_is_imported_only_when_exported(
    temp_repo: Path, declaration: str, exported: bool
) -> None:
    root = temp_repo / PROJECT
    _write(root, "pkg/util.js", declaration + JS_UTIL)
    store, updater = _index(root)
    if not exported:
        with pytest.raises(MoveRefused, match="SEP is not exported"):
            _move(root, store, updater, target="pkg/core.js", dry_run=True)
        return
    report = _move(root, store, updater, target="pkg/core.js", dry_run=True)
    assert "+import { SEP } from './util';" in report.diff
