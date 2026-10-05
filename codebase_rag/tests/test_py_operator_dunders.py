"""Python operators and protocols call the dunders they dispatch to.

Only `x[k]`, `k in x`, `len(x)` and truthiness produced a CALLS edge to the
operand's dunder. `a + b`, `a == b`, `-a`, `a += b`, calling an instance,
`for` over an object and `with obj:` all dispatch to a method at runtime
too, so those dunders showed no callers and `cgr check` could not flag a
caller of a removed `__add__`, `__call__` or `__enter__` (issue #2852).
"""

from __future__ import annotations

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

_M = """\
class Box:
    def __add__(self, o): return self
    def __radd__(self, o): return self
    def __iadd__(self, o): return self
    def __eq__(self, o): return True
    def __lt__(self, o): return True
    def __gt__(self, o): return True
    def __neg__(self): return self
    def __getitem__(self, i): return i
    def __call__(self, x): return x
    def __iter__(self): return iter([])
    def __aiter__(self): return self
    def __enter__(self): return self
    def __exit__(self, *a): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass


class Plain:
    pass


def helper(x):
    return x


def binary():
    a = Box(); b = Box()
    return a + b

def reflected():
    a = Box()
    return 1 + a

def augmented():
    a = Box(); b = Box()
    a += b

def equality():
    a = Box(); b = Box()
    return a == b

def ordering():
    a = Box(); b = Box()
    return a < b < a

def reflected_ordering():
    a = Box(); p = Plain()
    return p < a

def negate():
    a = Box()
    return -a

def call_instance():
    a = Box()
    return a(3)

def iterate():
    a = Box()
    for _x in a:
        pass

def comprehend():
    a = Box()
    return [x for x in a]

async def iterate_async():
    a = Box()
    async for _x in a:
        pass

def enter():
    a = Box()
    with a as w:
        return w

async def enter_async():
    a = Box()
    async with a:
        pass

def subscript():
    a = Box()
    return a[0]

def construct():
    return Box()

def class_reference():
    ref = Box
    return ref()

def plain_function():
    return helper(3)

def no_dunder():
    p = Plain()
    return p + p

def builtin_operands():
    xs = [1]
    return xs + xs

def identity():
    a = Box(); b = Box()
    return a is b

def opened(path):
    with open(path) as f:
        return f
"""


@pytest.fixture(scope="module")
def callees(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    root = tmp_path_factory.mktemp("ops") / "ops"
    root.mkdir()
    (root / "m.py").write_text(_M, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="ops",
    ).run(force=True)
    found: dict[str, set[str]] = {}
    for edge in store.keyed_edges:
        if edge[2] == cs.RelationshipType.CALLS.value:
            caller = str(edge[1]).removeprefix("ops.m.")
            found.setdefault(caller, set()).add(str(edge[4]).removeprefix("ops.m."))
    return found


def _dunders(callees: dict[str, set[str]], caller: str) -> set[str]:
    return {c for c in callees.get(caller, set()) if c.startswith("Box.__")}


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("binary", {"Box.__add__"}),
        ("reflected", {"Box.__radd__"}),
        ("augmented", {"Box.__iadd__"}),
        ("equality", {"Box.__eq__"}),
        ("ordering", {"Box.__lt__"}),
        ("reflected_ordering", {"Box.__gt__"}),
        ("negate", {"Box.__neg__"}),
        ("call_instance", {"Box.__call__"}),
        ("iterate", {"Box.__iter__"}),
        ("comprehend", {"Box.__iter__"}),
        ("iterate_async", {"Box.__aiter__"}),
        ("enter", {"Box.__enter__", "Box.__exit__"}),
        ("enter_async", {"Box.__aenter__", "Box.__aexit__"}),
    ],
)
def test_each_protocol_calls_its_dunder(
    callees: dict[str, set[str]], caller: str, expected: set[str]
) -> None:
    assert _dunders(callees, caller) == expected, callees.get(caller)


@pytest.mark.parametrize(
    "caller",
    [
        "construct",
        "class_reference",
        "plain_function",
        "no_dunder",
        "builtin_operands",
        "identity",
        "opened",
    ],
)
def test_no_dunder_is_invented(callees: dict[str, set[str]], caller: str) -> None:
    # Negatives: constructing a class, calling a class reference or a plain
    # function, an operand type without the dunder, builtin operands, an
    # identity test and a call-built context manager imply no Box dunder.
    assert _dunders(callees, caller) == set(), callees.get(caller)


def test_the_existing_subscript_edge_is_kept(callees: dict[str, set[str]]) -> None:
    assert _dunders(callees, "subscript") == {"Box.__getitem__"}
