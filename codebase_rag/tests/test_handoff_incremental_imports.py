"""An incremental run's project index lists every module's imports.

A fresh index parses the imports of every file. An incremental run parses
those of the files it re-parses and of the unchanged modules they reach, which
is all its own passes read, so an updater that did not index the tree before
(`cgr` run again, a new MCP session) held no import map for the others, and
the project index hand-off (`index.txt`) wrote no `imports` line for them. A
reused updater, as the watcher keeps, still held each from its earlier runs
(issue #3796). The hand-off now parses the missing maps on a copy of the
import state, so the graph the run writes stays as it is without one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codebase_rag.canonical_graph import canonical
from codebase_rag.config import settings
from codebase_rag.tests.test_graph_updater_index_engine import _write
from codebase_rag.tests.test_handoff import (
    _FILES,
    _FRONTENDS,
    _INDEX,
    _embedded_graph,
    _lines,
    _stamp_after_cache,
    _updater,
)

# One importer per language, each naming its provider in another package or
# directory, so each has an import map of its own.
TREE = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(name):\n    return name.upper()\n",
    "main.py": "from pkg.util import helper\n\n\ndef run():\n    return helper('y')\n",
    "web/util.js": "export function helper(x) {\n  return x + 1;\n}\n",
    "web/main.js": (
        'import { helper } from "./util.js";\n\n'
        "export function run() {\n  return helper(2);\n}\n"
    ),
    "app/util.ts": "export function helper(x: number): number {\n  return x + 1;\n}\n",
    "app/main.ts": (
        'import { helper } from "./util";\n\n'
        "export function run(): number {\n  return helper(2);\n}\n"
    ),
    "go.mod": "module example.com/shapes\n\ngo 1.22\n",
    "cmd/main.go": (
        'package main\n\nimport "example.com/shapes/geo"\n\n'
        "func main() {\n\tgeo.Area(2)\n}\n"
    ),
    "geo/shape.go": "package geo\n\nfunc Area(side int) int {\n\treturn side * side\n}\n",
    "src/demo/util/Util.java": (
        "package demo.util;\n\npublic class Util {\n"
        "    public static int helper(int x) { return x + 1; }\n}\n"
    ),
    "src/demo/Main.java": (
        "package demo;\n\nimport demo.util.Util;\n\npublic class Main {\n"
        "    public int run() { return Util.helper(2); }\n}\n"
    ),
}
IMPORTERS = {
    "demo.main",
    "demo.web.main",
    "demo.app.main",
    "demo.cmd.main",
    "demo.src.demo.Main",
}
# Each language's provider gains a definition: the run re-parses it and its
# importer, and leaves the other languages' importers alone.
EDITS = {
    "pkg/util.py": TREE["pkg/util.py"] + "\n\ndef extra():\n    return 1\n",
    "web/util.js": TREE["web/util.js"]
    + "\nexport function extra() {\n  return 1;\n}\n",
    "app/util.ts": TREE["app/util.ts"]
    + "\nexport function extra(): number {\n  return 1;\n}\n",
    "geo/shape.go": TREE["geo/shape.go"] + "\nfunc Extra() int {\n\treturn 1\n}\n",
    "src/demo/util/Util.java": TREE["src/demo/util/Util.java"].replace(
        "}\n}\n", "}\n    public static int extra() { return 1; }\n}\n"
    ),
}
# Maps parsing alone leaves unfinished, which the run finishes once every
# module is known: a Rust file's inline `mod` `use`s, keyed on the `mod`,
# and a Scala import of a package the project declares (`shop.util`, in no
# `shop/` directory), pointed at the module declaring it (#2450). Beside
# them a notebook, whose imports are its code cells'. The edit reaches none.
NOTEBOOK = json.dumps(
    {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": None,
                "metadata": {},
                "outputs": [],
                "source": [
                    "from pkg.util import helper\n",
                    "\n",
                    "\n",
                    "def summarize():\n",
                    "    return helper('x')\n",
                ],
            }
        ],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
)
FINISHED_TREE = {
    "pkg/__init__.py": "",
    "pkg/util.py": TREE["pkg/util.py"],
    "other.py": "def lonely():\n    return 1\n",
    "crab/Cargo.toml": '[package]\nname = "crab"\nversion = "0.1.0"\n',
    "crab/src/util.rs": "pub fn helper(x: i32) -> i32 {\n    x + 1\n}\n",
    "crab/src/lib.rs": (
        "pub mod util;\n\nuse crate::util::helper;\n\n"
        "pub fn top() -> i32 {\n    helper(2)\n}\n\n"
        "mod inner {\n    use crate::util::helper as twice;\n\n"
        "    pub fn run() -> i32 {\n        twice(1)\n    }\n}\n"
    ),
    "scala/Tools.scala": (
        "package shop.util\n\nobject Tools {\n  def helper(x: Int): Int = x + 1\n}\n"
    ),
    "scala/Main.scala": (
        "package shop\n\nimport shop.util.Tools\n\n"
        "object Main {\n  def run(): Int = Tools.helper(2)\n}\n"
    ),
    "analysis.ipynb": NOTEBOOK,
}
FINISHED_IMPORTERS = {
    "demo.crab.src.lib",
    "demo.crab.src.lib.inner",
    "demo.scala.Main",
    "demo.analysis.ipynb",
}
FINISHED_EDIT = ("other.py", "def lonely():\n    return 2\n")


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


def _sync(
    repo: Path,
    graph: Path,
    edit: tuple[str, str],
    *,
    reused: bool,
    handoffs: Path | None,
) -> list[str]:
    """Index `repo`, write `edit` (a path and its new text), and sync with a
    new updater or the same one; the graph the sync leaves."""
    edited, text = edit
    with _embedded_graph(graph) as ingestor:
        # The checkout's own state directory, which the stamp reads.
        updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
        updater.run()
        (repo / edited).write_text(text, encoding="utf-8")
        _stamp_after_cache(repo, edited)
        if not reused:
            updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
        updater.run()
        return canonical(ingestor.export_graph_to_dict(), repo)


def _fresh_index(repo: Path, scratch: Path) -> list[str]:
    handoffs = scratch / "fresh"
    with _embedded_graph(scratch / "fresh.sqlite") as ingestor:
        _updater(
            repo, ingestor, state_dir=scratch / "fresh-state", handoff_dir=handoffs
        ).run(force=True)
    return _lines(handoffs / _INDEX)


def _imports(index: list[str]) -> list[str]:
    return [line for line in index if line.startswith("imports\t")]


def _imports_keys(index: list[str]) -> set[str]:
    return {line.split("\t")[1] for line in _imports(index)}


@pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
@pytest.mark.parametrize("edited", sorted(EDITS))
def test_it_equals_a_fresh_index_s(tmp_path: Path, edited: str, reused: bool) -> None:
    repo = _write(tmp_path / "repo", TREE)
    synced = tmp_path / "synced"
    _sync(
        repo,
        tmp_path / "graph.sqlite",
        (edited, EDITS[edited]),
        reused=reused,
        handoffs=synced,
    )
    fresh = _fresh_index(repo, tmp_path)

    # The sync was incremental: a full build names no file it re-parses.
    assert f'reparse\t{edited}\t{{"reason":"changed"}}' in _lines(synced / _FILES)
    assert _imports_keys(fresh) == IMPORTERS
    assert _lines(synced / _INDEX) == fresh


@pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
def test_its_imports_equal_a_fresh_index_s_where_the_run_finishes_them(
    tmp_path: Path, reused: bool
) -> None:
    repo = _write(tmp_path / "repo", FINISHED_TREE)
    synced = tmp_path / "synced"
    _sync(
        repo, tmp_path / "graph.sqlite", FINISHED_EDIT, reused=reused, handoffs=synced
    )
    fresh = _fresh_index(repo, tmp_path)

    edited, _text = FINISHED_EDIT
    assert f'reparse\t{edited}\t{{"reason":"changed"}}' in _lines(synced / _FILES)
    assert _imports_keys(fresh) == FINISHED_IMPORTERS
    # The imports alone: a bodied inline `mod` is read back from the graph as
    # a module with its file's path, which a fresh index only declares, so an
    # incremental index has a `module` line for it that a fresh one lacks.
    assert _imports(_lines(synced / _INDEX)) == _imports(fresh)


@pytest.mark.parametrize("edited", sorted(EDITS))
def test_the_sync_writes_the_same_graph_without_a_hand_off(
    tmp_path: Path, edited: str
) -> None:
    # The maps the hand-off parses go into a copy of the import state, so the
    # passes after it resolve exactly what they resolve without one.
    graphs = [
        _sync(
            _write(tmp_path / name / "repo", TREE),
            tmp_path / name / "graph.sqlite",
            (edited, EDITS[edited]),
            reused=False,
            handoffs=handoffs,
        )
        for name, handoffs in (("with", tmp_path / "handoffs"), ("without", None))
    ]

    assert _imports_keys(_lines(tmp_path / "handoffs" / _INDEX)) == IMPORTERS
    assert graphs[0] == graphs[1]


def test_finishing_the_maps_leaves_the_graph_as_without_a_hand_off(
    tmp_path: Path,
) -> None:
    # The run's own last steps over the maps are taken again on the copy, not
    # on the run's state.
    graphs = [
        _sync(
            _write(tmp_path / name / "repo", FINISHED_TREE),
            tmp_path / name / "graph.sqlite",
            FINISHED_EDIT,
            reused=False,
            handoffs=handoffs,
        )
        for name, handoffs in (("with", tmp_path / "handoffs"), ("without", None))
    ]

    assert _imports_keys(_lines(tmp_path / "handoffs" / _INDEX)) == (FINISHED_IMPORTERS)
    assert graphs[0] == graphs[1]
