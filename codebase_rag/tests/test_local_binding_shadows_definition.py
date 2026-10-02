"""A local binding shadows a same-named definition for the whole body.

Issue #2666. A parameter or local named like a function of its module, or
like a method of its class, still resolved to that definition as `exact`, so
`key(item)` inside `apply(key, item)`, `f = key` and `sorted(items, key=key)`
were CALLS/REFERENCES edges to the module's `key`, and `cgr rename key
by_priority` rewrote them. The #1907 check covered only import-map names used
as a receiver. The same gap reached other modules through the simple-name
trie as `heuristic` edges into test helpers (the issue's follow-up comment).

Every defect row pairs with controls that must keep their edge: the module
function used where nothing shadows it, a nested def of the same name, a
`global` declaration and a comprehension whose variable does not cover every
use of the name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_JOBS = """\
from typing import Callable


def key(item):
    return item.priority


def pick(items, key):
    return sorted(items, key=key)


def apply(key, item):
    return key(item)


def apply_ref(key):
    f = key
    return f


def apply_arg(items, key):
    return list(map(key, items))


def local_key(items):
    key = lambda i: i.name
    return sorted(items, key=key)


def apply_typed(key: Callable[[int], int], item: int) -> int:
    return key(item)


def for_key(keys):
    for key in keys:
        key(1)


def outer(key):
    def inner(x):
        return key(x)

    return inner


def lambda_param(fns):
    return list(map(lambda key: key(1), fns))


def uses_module_key(items):
    return sorted(items, key=key)


def calls_module_key(item):
    return key(item)


def nested_def_shadow(item):
    def key(i):
        return i

    return key(item)


def declared_global(item):
    global key
    return key(item)


def via_alias(item):
    f = key
    return f(item)


def comprehension_does_not_leak(items):
    firsts = [key for key in items]
    return key(firsts[0])


class Runner:
    def __init__(self, run):
        self._run = run

    def run(self):
        return self._run()

    def start(self):
        return self.run()
"""

_ITEMS = """\
class Item:
    def __init__(self, value):
        self.value = value


def wrap(values):
    return [Item(c) for c in values]


def first(b, sink):
    sink.write(b)
    return Item(b)
"""

_TEST_CHAIN = """\
def test_chain():
    def b():
        pass

    def c():
        pass

    return b, c
"""

Edge = tuple[str, str, str]


def _build(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / "lib").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "jobs.py").write_text(_JOBS, encoding="utf-8")
    (repo / "lib" / "__init__.py").touch()
    (repo / "lib" / "items.py").write_text(_ITEMS, encoding="utf-8")
    (repo / "tests" / "test_chain.py").write_text(_TEST_CHAIN, encoding="utf-8")
    return repo


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> dict[Edge, str]:
    """(caller, rel, target) -> resolution label, for CALLS and REFERENCES."""
    repo = _build(tmp_path_factory.mktemp("shadow"))
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(ingestor=store, repo_path=repo, parsers=parsers, queries=queries).run(
        force=True
    )
    kept = (cs.RelationshipType.CALLS.value, cs.RelationshipType.REFERENCES.value)
    edges: dict[Edge, str] = {}
    for edge in store.keyed_edges:
        _sl, src, rel, _tl, tgt, _site = edge
        if rel in kept:
            props = store.edge_props.get(edge, {})
            edges[(str(src), rel, str(tgt))] = str(
                props.get(cs.KEY_RESOLUTION, cs.EdgeResolution.EXACT.value)
            )
    return edges


def _targets_from(graph: dict[Edge, str], caller: str) -> set[str]:
    return {tgt for src, _rel, tgt in graph if src == f"proj.{caller}"}


# (caller, the definition its local shadows): no CALLS or REFERENCES edge.
SHADOWED = [
    pytest.param("jobs.apply", "jobs.key", id="parameter-call"),
    pytest.param("jobs.apply_ref", "jobs.key", id="parameter-reference"),
    pytest.param("jobs.pick", "jobs.key", id="parameter-keyword-argument"),
    pytest.param("jobs.apply_arg", "jobs.key", id="parameter-positional-argument"),
    pytest.param("jobs.local_key", "jobs.key", id="local-assignment"),
    pytest.param("jobs.apply_typed", "jobs.key", id="typed-callable-parameter"),
    pytest.param("jobs.for_key", "jobs.key", id="for-target"),
    pytest.param("jobs.outer", "jobs.key", id="parameter-of-closure-factory"),
    pytest.param("jobs.outer.inner", "jobs.key", id="enclosing-def-parameter"),
    pytest.param("jobs.lambda_param", "jobs.key", id="lambda-parameter"),
    pytest.param("jobs.Runner.__init__", "jobs.Runner.run", id="method-named-param"),
    pytest.param(
        "lib.items.wrap", "tests.test_chain.test_chain.c", id="comprehension-variable"
    ),
    pytest.param(
        "lib.items.first", "tests.test_chain.test_chain.b", id="parameter-via-trie"
    ),
]


@pytest.mark.parametrize(("caller", "shadowed"), SHADOWED)
def test_a_local_binding_never_resolves_to_the_definition_it_shadows(
    graph: dict[Edge, str], caller: str, shadowed: str
) -> None:
    assert f"proj.{shadowed}" not in _targets_from(graph, caller)


# Negative: what must not change.

KEPT = [
    pytest.param(
        "jobs.uses_module_key", "CALLS", "jobs.key", id="module-function-argument"
    ),
    pytest.param(
        "jobs.calls_module_key", "CALLS", "jobs.key", id="module-function-call"
    ),
    pytest.param(
        "jobs.nested_def_shadow", "CALLS", "jobs.nested_def_shadow.key", id="nested-def"
    ),
    pytest.param("jobs.declared_global", "CALLS", "jobs.key", id="declared-global"),
    pytest.param("jobs.via_alias", "CALLS", "jobs.key", id="local-alias-of-function"),
    pytest.param(
        "jobs.comprehension_does_not_leak",
        "CALLS",
        "jobs.key",
        id="comprehension-variable-used-outside",
    ),
    pytest.param(
        "lib.items.wrap", "CALLS", "lib.items.Item.__init__", id="constructor"
    ),
    pytest.param(
        "lib.items.first", "CALLS", "lib.items.Item.__init__", id="ctor-param"
    ),
    pytest.param(
        "tests.test_chain.test_chain",
        "REFERENCES",
        "tests.test_chain.test_chain.b",
        id="nested-def-reference",
    ),
]


@pytest.mark.parametrize(("caller", "rel", "target"), KEPT)
def test_an_unshadowed_use_keeps_its_exact_edge(
    graph: dict[Edge, str], caller: str, rel: str, target: str
) -> None:
    edge = (f"proj.{caller}", rel, f"proj.{target}")
    assert edge in graph, sorted(e for e in graph if e[0] == edge[0])
    assert graph[edge] == cs.EdgeResolution.EXACT.value


def test_a_self_method_call_still_resolves(graph: dict[Edge, str]) -> None:
    # `self.run()` is the method, whatever a parameter named `run` does in
    # another method. Its label is #2475's business, so only presence counts.
    assert ("proj.jobs.Runner.start", "CALLS", "proj.jobs.Runner.run") in graph


def test_the_nested_def_shadow_does_not_also_reach_the_module_function(
    graph: dict[Edge, str],
) -> None:
    assert "proj.jobs.key" not in _targets_from(graph, "jobs.nested_def_shadow")
