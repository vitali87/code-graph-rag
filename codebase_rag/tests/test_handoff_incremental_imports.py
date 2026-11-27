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


@pytest.fixture(autouse=True)
def _frontends_need_no_compiler(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _FRONTENDS.items():
        monkeypatch.setattr(settings, name, value)


def _sync(
    repo: Path, graph: Path, edited: str, *, reused: bool, handoffs: Path | None
) -> list[str]:
    """Index `repo`, edit `edited`, and sync with a new updater or the same
    one; the graph the sync leaves."""
    with _embedded_graph(graph) as ingestor:
        # The checkout's own state directory, which the stamp reads.
        updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
        updater.run()
        (repo / edited).write_text(EDITS[edited], encoding="utf-8")
        _stamp_after_cache(repo, edited)
        if not reused:
            updater = _updater(repo, ingestor, state_dir=None, handoff_dir=handoffs)
        updater.run()
        return canonical(ingestor.export_graph_to_dict(), repo)


def _imports_keys(index: list[str]) -> set[str]:
    return {line.split("\t")[1] for line in index if line.startswith("imports\t")}


@pytest.mark.parametrize("reused", [False, True], ids=["new", "reused"])
@pytest.mark.parametrize("edited", sorted(EDITS))
def test_it_equals_a_fresh_index_s(tmp_path: Path, edited: str, reused: bool) -> None:
    repo = _write(tmp_path / "repo", TREE)
    synced = tmp_path / "synced"
    _sync(repo, tmp_path / "graph.sqlite", edited, reused=reused, handoffs=synced)
    fresh = tmp_path / "fresh"
    with _embedded_graph(tmp_path / "fresh.sqlite") as ingestor:
        _updater(
            repo, ingestor, state_dir=tmp_path / "fresh-state", handoff_dir=fresh
        ).run(force=True)

    # The sync was incremental: a full build names no file it re-parses.
    assert f'reparse\t{edited}\t{{"reason":"changed"}}' in _lines(synced / _FILES)
    assert _imports_keys(_lines(fresh / _INDEX)) == IMPORTERS
    assert _lines(synced / _INDEX) == _lines(fresh / _INDEX)


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
            edited,
            reused=False,
            handoffs=handoffs,
        )
        for name, handoffs in (("with", tmp_path / "handoffs"), ("without", None))
    ]

    assert _imports_keys(_lines(tmp_path / "handoffs" / _INDEX)) == IMPORTERS
    assert graphs[0] == graphs[1]
