"""Issue #2893: a JS/TS method call on a free function's result binds the
method of the class the function returns.

`make().handle()` names its receiver's type through `make`'s declared (or
constructed) return, and the chain resolver already types a factory call
from a recorded return, but no JS/TS function's return was ever recorded,
so the call bound nothing: a method reached only through a factory's result
was reported dead. The hoisted `const s = make(); s.handle()` bound only by
a unique-method-name fallback, which gives up as soon as two classes share
the method's name.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from evals.cgr_graph import _StatefulIngestor

SOURCE = """\
import { makeWidget } from './w';

class Service { handle(): number { return 1; } }
class Other { handle(): number { return 2; } }
class Builder {
  with(): Builder { return this; }
  build(): Service { return new Service(); }
}

function make(): Service { return new Service(); }
function makeOther(): Other { return new Other(); }
function makeWith(n: number, m: number): Service { return new Service(); }
function makeMaybe(): Service | undefined { return new Service(); }
const makeArrow = (): Service => new Service();
function makeInferred() { return new Service(); }
const makeArrowInferred = () => (new Service());
function builder(): Builder { return new Builder(); }
function makeAll(): Service[] { return [new Service()]; }
async function makeAsync() { return new Service(); }
async function makeAsyncTyped(): Promise<Service> { return new Service(); }
function makeMixed(flag: boolean) {
  if (flag) { return new Service(); }
  return new Other();
}
function makeUnknown(x: unknown) { return x; }

function useFactory() { return make().handle(); }
function useOther() { return makeOther().handle(); }
function useArgs() {
  return makeWith(
    1,
    2,
  ).handle();
}
function useMaybe() { return makeMaybe().handle(); }
function useArrow() { return makeArrow().handle(); }
function useInferred() { return makeInferred().handle(); }
function useArrowInferred() { return makeArrowInferred().handle(); }
function useBuilder() { return builder().build(); }
function useImported() { return makeWidget().spin(); }

function useAll() { return makeAll().handle(); }
function useAsync() { return makeAsync().handle(); }
function useAsyncTyped() { return makeAsyncTyped().handle(); }
function useMixed() { return makeMixed(true).handle(); }
function useUnknown() { return makeUnknown(1).handle(); }
function useNew() { const s = new Service(); return s.handle(); }
"""

WIDGET = """\
import { Widget } from './widget';

export function makeWidget(): Widget { return new Widget(); }
"""

WIDGET_CLASS = """\
export class Widget { spin(): number { return 3; } }
"""

USE_WIDGET = """\
import { makeWidget } from './w';

export function useWidget() { return makeWidget().spin(); }
"""

JS_SOURCE = """\
class Gadget { tick() { return 1; } }
class Gizmo { tick() { return 2; } }
function makeGadget() { return new Gadget(); }
function useGadget() { return makeGadget().tick(); }
"""


# `Service` from an unindexed package, beside the project's own `Service`
# in m.ts: the factory returns the package's class, which is not indexed.
EXTERNAL = """\
import { Service } from 'package';

export function makeExternal() { return new Service(); }
export function makeExternalTyped(): Service { return new Service(); }
"""

EXTERNAL_USER = """\
import { makeExternal, makeExternalTyped } from './ext';

export function useExternal() { return makeExternal().handle(); }
export function useExternalTyped() { return makeExternalTyped().handle(); }
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("tschain") / "tschain"
    _write(root, "m.ts", SOURCE)
    _write(root, "w.ts", WIDGET)
    _write(root, "widget.ts", WIDGET_CLASS)
    _write(root, "g.js", JS_SOURCE)
    _write(root, "ext.ts", EXTERNAL)
    _write(root, "x.ts", EXTERNAL_USER)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, module: str, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}{module}.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "factory", "method"),
    [
        ("useFactory", "m.make", "m.Service.handle"),
        ("useOther", "m.makeOther", "m.Other.handle"),
        ("useArgs", "m.makeWith", "m.Service.handle"),
        ("useMaybe", "m.makeMaybe", "m.Service.handle"),
        ("useArrow", "m.makeArrow", "m.Service.handle"),
        ("useInferred", "m.makeInferred", "m.Service.handle"),
        ("useArrowInferred", "m.makeArrowInferred", "m.Service.handle"),
        ("useBuilder", "m.builder", "m.Builder.build"),
        ("useImported", "w.makeWidget", "widget.Widget.spin"),
    ],
)
def test_a_call_on_a_factory_result_binds_the_returned_class(
    graph: RecordedGraph, caller: str, factory: str, method: str
) -> None:
    assert _callees(graph, "m", caller) == {factory: "exact", method: "exact"}


def test_plain_javascript_types_the_constructed_return(graph: RecordedGraph) -> None:
    assert _callees(graph, "g", "useGadget") == {
        "g.makeGadget": "exact",
        "g.Gadget.tick": "exact",
    }


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "factory"),
    [
        ("useAll", "m.makeAll"),
        ("useAsync", "m.makeAsync"),
        ("useAsyncTyped", "m.makeAsyncTyped"),
        ("useMixed", "m.makeMixed"),
        ("useUnknown", "m.makeUnknown"),
    ],
    ids=["array-return", "async", "promise-return", "two-classes", "untyped"],
)
def test_a_result_that_is_no_single_instance_binds_no_method(
    graph: RecordedGraph, caller: str, factory: str
) -> None:
    assert _callees(graph, "m", caller) == {factory: "exact"}


@pytest.mark.parametrize(
    ("caller", "factory"),
    [
        ("useExternal", "ext.makeExternal"),
        ("useExternalTyped", "ext.makeExternalTyped"),
    ],
    ids=["constructed", "annotated"],
)
def test_an_unindexed_returned_class_binds_no_same_named_local_class(
    graph: RecordedGraph, caller: str, factory: str
) -> None:
    # The factory's class is the package's `Service`; m.ts's `Service` is
    # another class that happens to share its name (Greptile, PR #2962).
    assert _callees(graph, "x", caller) == {factory: "exact"}


def test_a_factory_left_unchanged_by_a_reingest_still_types_its_result(
    tmp_path: Path,
) -> None:
    # A scoped re-ingest re-parses the caller alone; the factory's file is
    # rehydrated from the graph, and its return is still read (CodeRabbit,
    # PR #2962).
    root = tmp_path / "rehyd"
    _write(root, "w.ts", WIDGET)
    _write(root, "widget.ts", WIDGET_CLASS)
    _write(root, "use.ts", USE_WIDGET)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()

    def updater() -> GraphUpdater:
        return GraphUpdater(
            ingestor=store,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name="rehyd",
        )

    updater().run(force=True)
    _write(root, "use.ts", USE_WIDGET + "\nexport const touched = 1;\n")
    updater().reingest(["use.ts"])

    callees = {
        str(edge[4])
        for edge in store.keyed_edges
        if edge[2] == cs.RelationshipType.CALLS and edge[1] == "rehyd.use.useWidget"
    }
    assert "rehyd.widget.Widget.spin" in callees


def test_a_constructed_local_still_binds(graph: RecordedGraph) -> None:
    assert _callees(graph, "m", "useNew") == {"m.Service.handle": "exact"}
