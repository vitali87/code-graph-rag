"""A JS/TS method call does not also call a same-named top-level function.

The twin fan-out exists for one function registered under two names
(`View.prototype.lookup = function lookup()` is `View.lookup` and module-flat
`view.lookup`). It matched twins by leaf name and parent path, so `Store.get`
paired with its module's own `function get()`: `s.get("a")` and
`new Store().get("b")` each got a second `exact` edge to the free function,
and `cgr rename` of that function rewrote the method calls (issue #3174).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships
from codebase_rag.tests.test_rename_op import _index
from evals.cgr_graph import _StatefulIngestor

_STORE = """\
export function get(key: string): string { return "module-level " + key; }

export class Store {
  get(key: string): string { return "method " + key; }
}
"""
_USE = """\
import { Store, get } from "./store";
export function run(s: Store): string { return s.get("a"); }
export function viaNew(): string { return new Store().get("b"); }
export function bare(): string { return get("c"); }
"""
_PLAIN = """\
function parse(text) { return text.trim(); }
class Parser {
  parse(text) { return text; }
  run(text) { return this.parse(text); }
}
module.exports = { parse, Parser };
"""
_LIFECYCLE = """\
export function dispose(items: { dispose(): void }[]): void {
  for (const item of items) item.dispose();
}

export class DisposableStore {
  dispose(): void {}
}

export function teardown(store: DisposableStore): void {
  store.dispose();
}
"""
_NS = """\
export namespace Outer {
  export namespace Inner {
    export function deep(): number { return 1; }
  }
}

export function deep(): number { return 2; }

export function viaNamespace(): number { return Outer.Inner.deep(); }
"""
_VIEW = """\
function View(name) {
  this.name = name
  this.lookup(name)
}
View.prototype.lookup = function lookup(name) {
  return name
}
module.exports = View
"""
_FILES = {
    "src/store.ts": _STORE,
    "src/use.ts": _USE,
    "src/plain.js": _PLAIN,
    "src/lifecycle.ts": _LIFECYCLE,
    "src/ns.ts": _NS,
    "src/view.js": _VIEW,
}

_Calls = dict[str, set[str]]


def _write(root: Path) -> None:
    for rel, text in _FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _short(qn: str) -> str:
    return qn.split(".src.", 1)[-1]


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> _Calls:
    root = tmp_path_factory.mktemp("js3174") / "tstwin"
    _write(root)
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="typescript")
    out: _Calls = {}
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        out.setdefault(_short(str(c.args[0][2])), set()).add(_short(str(c.args[2][2])))
    return out


@pytest.mark.parametrize(
    ("caller", "callees"),
    [
        ("use.run", {"store.Store.get"}),
        ("use.viaNew", {"store.Store.get"}),
        ("plain.Parser.run", {"plain.Parser.parse"}),
        ("lifecycle.teardown", {"lifecycle.DisposableStore.dispose"}),
        ("ns.viaNamespace", {"ns.Outer.Inner.deep"}),
    ],
)
def test_a_member_call_reaches_only_the_method(
    calls: _Calls, caller: str, callees: set[str]
) -> None:
    assert calls.get(caller) == callees, calls


def test_the_free_functions_keep_their_own_callers(calls: _Calls) -> None:
    # Negatives: a bare call still reaches the module-level function, and
    # each one-definition-two-names prototype method keeps both edges.
    assert calls.get("use.bare") == {"store.get"}, calls
    assert calls.get("view.View") == {"view.View.lookup", "view.lookup"}, calls


def test_rename_of_the_free_function_leaves_the_method_calls(
    temp_repo: Path,
) -> None:
    _write(temp_repo)
    graph = _index(temp_repo, MagicMock())
    report = rename(
        temp_repo,
        graph.fetch_all,
        graph.project,
        f"{graph.project}.src.store.get",
        "fetchValue",
    )
    assert report.applied, report.message
    use = (temp_repo / "src/use.ts").read_text()
    assert 'return fetchValue("c");' in use, use
    assert 's.get("a")' in use and 'new Store().get("b")' in use, use
    assert "  get(key: string)" in (temp_repo / "src/store.ts").read_text()


def test_an_incremental_sync_keeps_the_prototype_twins(temp_repo: Path) -> None:
    # Re-parsing only the edited files leaves view.js to the graph's rows, so
    # its one-definition pair has to come back from them; the unrelated
    # `Store.get` / `get` pair must not. The result matches a clean index.
    parsers, queries = load_parsers()
    if "typescript" not in parsers:
        pytest.skip("typescript parser not available")

    def sync(store: _StatefulIngestor, force: bool) -> _Calls:
        GraphUpdater(
            ingestor=store, repo_path=temp_repo, parsers=parsers, queries=queries
        ).run(force=force)
        out: _Calls = {}
        for _, source, rel, _, target in store.edges:
            if rel == cs.RelationshipType.CALLS.value:
                out.setdefault(_short(str(source)), set()).add(_short(str(target)))
        return out

    _write(temp_repo)
    store = _StatefulIngestor()
    sync(store, force=True)
    (temp_repo / "src/use.ts").write_text(_USE + "// touched\n", encoding="utf-8")
    (temp_repo / "src/caller.js").write_text(
        'const View = require("./view");\n'
        "function make() { return new View('y').lookup('z'); }\n"
        "module.exports = { make };\n",
        encoding="utf-8",
    )
    after = sync(store, force=False)
    clean = sync(_StatefulIngestor(), force=True)
    assert after.get("use.run") == {"store.Store.get"}, after
    assert {"view.View.lookup", "view.lookup"} <= after.get("caller.make", set())
    assert after.get("caller.make") == clean.get("caller.make"), (after, clean)
