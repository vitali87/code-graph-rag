"""Issue #2945: returning a nested function is not a call of it.

Every function that defines a closure and returns it (a decorator's
`wrapper`, a factory's product) got a CALLS edge to it, with no line, beside
the located REFERENCES edge of its `return`: `cgr graph callees` of every
decorator listed its wrapper, and `callers` of the wrapper listed the
decorator. The edge only kept the closure reachable for dead-code, which the
REFERENCES edge already does.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

PY = """\
import functools


def log_calls(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)
    return wrapper


@log_calls
def greet(name):
    return "hi " + name


def make(flag):
    if flag:
        def impl():
            return 1
    else:
        def impl():
            return 2
    return impl


def eager():
    def inner():
        return 1
    return inner()


def make_adder():
    def add(x):
        return x + 1
    return add


def use_adder():
    f = make_adder()
    return f(1)


def _unused_factory():
    def orphan():
        return 1
    return orphan
"""

JS = """\
export function makeLogger(fn) {
  function wrapper(...a) { return fn(...a); }
  return wrapper;
}
export const arrowFactory = (fn) => {
  const inner = (...a) => fn(...a);
  return inner;
};
"""

TS = """\
type F = (...a: unknown[]) => unknown;
export function makeLogger(fn: F): F {
  function wrapper(...a: unknown[]) { return fn(...a); }
  return wrapper;
}
export function castFactory(fn: F): F {
  function wrapper(...a: unknown[]) { return fn(...a); }
  return wrapper as unknown as F;
}
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("deco") / "deco"
    _write(root, "deco.py", PY)
    _write(root, "f.js", JS)
    _write(root, "t.ts", TS)
    return _index(root, MagicMock())


def _edges(graph: RecordedGraph, rel: str) -> dict[tuple[str, str], object]:
    prefix = f"{graph.project}."
    return {
        (src.removeprefix(prefix), dst.removeprefix(prefix)): props.get("line")
        for src, kind, dst, props in graph.edges
        if kind == rel
    }


RETURNED = [
    ("deco.log_calls", "deco.log_calls.wrapper"),
    ("deco.make", "deco.make.impl"),
    ("f.makeLogger", "f.makeLogger.wrapper"),
    ("f.arrowFactory", "f.arrowFactory.inner"),
    ("t.makeLogger", "t.makeLogger.wrapper"),
    ("t.castFactory", "t.castFactory.wrapper"),
]


@pytest.mark.parametrize(("outer", "closure"), RETURNED)
def test_a_returned_closure_is_not_called_by_its_maker(
    graph: RecordedGraph, outer: str, closure: str
) -> None:
    assert (outer, closure) not in _edges(graph, cs.RelationshipType.CALLS)


def test_no_twin_of_a_returned_closure_is_called(graph: RecordedGraph) -> None:
    calls = _edges(graph, cs.RelationshipType.CALLS)
    assert not [pair for pair in calls if pair[0] == "deco.make"]


# Negative: what must not change.


@pytest.mark.parametrize(("outer", "closure"), RETURNED)
def test_the_return_is_still_a_located_reference(
    graph: RecordedGraph, outer: str, closure: str
) -> None:
    line = _edges(graph, cs.RelationshipType.REFERENCES).get((outer, closure))
    assert isinstance(line, int)


def test_a_closure_called_before_it_is_returned_keeps_its_edge(
    graph: RecordedGraph,
) -> None:
    # `return inner()` calls it, on that line.
    calls = _edges(graph, cs.RelationshipType.CALLS)
    assert isinstance(calls.get(("deco.eager", "deco.eager.inner")), int)


class _DeadCodeGraph:
    """The dead-code queries answered from what the indexer emitted."""

    def __init__(self, graph: RecordedGraph) -> None:
        labels = {qn: props[cs.KEY_LABEL] for qn, props in graph.nodes.items()}
        self._nodes: list[ResultRow] = [
            {
                cs.KEY_LABEL: props[cs.KEY_LABEL],
                cs.KEY_QUALIFIED_NAME: qn,
                cs.KEY_NAME: props.get(cs.KEY_NAME),
                cs.KEY_PATH: props.get(cs.KEY_PATH),
                cs.KEY_START_LINE: props.get(cs.KEY_START_LINE),
                cs.KEY_END_LINE: props.get(cs.KEY_END_LINE),
                cs.KEY_DECORATORS: props.get(cs.KEY_DECORATORS, []),
                cs.KEY_IS_EXPORTED: props.get(cs.KEY_IS_EXPORTED, False),
                cs.KEY_OVERRIDES_EXTERNAL: props.get(cs.KEY_OVERRIDES_EXTERNAL, False),
            }
            for qn, props in graph.nodes.items()
            if props[cs.KEY_LABEL]
            in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
        ]
        self._rels: list[ResultRow] = [
            {
                cs.KEY_FROM_LABEL: labels.get(src),
                cs.KEY_FROM_QN: src,
                cs.KEY_REL_TYPE: rel,
                cs.KEY_TO_LABEL: labels.get(dst),
                cs.KEY_TO_QN: dst,
            }
            for src, rel, dst, _props in graph.edges
        ]

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._nodes if query == cq.CYPHER_DEAD_CODE_NODES else self._rels


def test_dead_code_still_keeps_a_returned_closure_alive(graph: RecordedGraph) -> None:
    config = default_dead_code_config(include_tests=True, include_classes=False)
    reported = {
        str(row[cs.KEY_QUALIFIED_NAME]).removeprefix(f"{graph.project}.")
        for row in collect_dead_code(_DeadCodeGraph(graph), graph.project, config)
    }

    alive = {closure for _outer, closure in RETURNED} | {
        "deco.make.impl@21",
        "deco.make_adder.add",
    }
    assert not reported & alive
    # A factory nothing reaches does not revive its closure.
    assert {"deco._unused_factory", "deco._unused_factory.orphan"} <= reported
