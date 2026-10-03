"""Issue #2859: a method call on a Python builtin value binds to no first-party method.

`"-".join(parts)`, `{}.get(k)`, `d = {}; d.get(k)` and `self.cache = {}; self.cache.get(k)`
call `str.join`, `dict.get`, ... but nothing typed a literal receiver, so
the call fell to the bare-name trie and bound to whichever first-party class
defines a method of that name: rich's `Text.join` collected every
`"".join(...)` in the project as a caller.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

LIB = (
    "class Text:\n    def join(self, parts):\n        return parts\n\n\n"
    "class Bag:\n"
    "    def get(self, k):\n        return k\n\n"
    "    def append(self, x):\n        return x\n\n"
    "    def update(self, x):\n        return x\n\n"
    "    def strip(self):\n        return 1\n\n"
    "    def count(self, x):\n        return x\n"
)

APP = (
    "from lib import Bag\n\n\n"
    'def s_join():\n    return "-".join(["a", "b"])\n\n\n'
    'def fs_join(x):\n    return f"{x}".join([])\n\n\n'
    'def d_get():\n    return {}.get("k")\n\n\n'
    "def l_append():\n    return [].append(1)\n\n\n"
    "def dc_get():\n    return {k: 1 for k in 'ab'}.get('a')\n\n\n"
    "def b_strip():\n    return b'x'.strip()\n\n\n"
    "def set_update():\n    return {1, 2}.update([3])\n\n\n"
    "def tuple_count():\n    return (1, 2).count(1)\n\n\n"
    'def local_get():\n    d = {}\n    return d.get("k")\n\n\n'
    "def local_append():\n    items = []\n    items.append(1)\n    return items\n\n\n"
    'def local_strip():\n    s = "x"\n    return s.strip()\n\n\n'
    "def annotated(d: dict):\n    return d.get(1)\n\n\n"
    "class App:\n"
    "    def __init__(self):\n"
    "        self.cache = {}\n        self.log = []\n        self.bag = Bag()\n\n"
    "    def lookup(self, k):\n        return self.cache.get(k)\n\n"
    "    def record(self, x):\n        self.log.append(x)\n\n"
    "    def fetch(self, k):\n        return self.bag.get(k)\n\n\n"
    "def real_bag():\n    b = Bag()\n    return b.get(1)\n\n\n"
    "def untyped(x):\n    return x.get(1)\n\n\n"
    "def loop_keys():\n    d = {}\n    for k in d:\n        k.get(1)\n\n\n"
    "def loop_bags():\n    for b in [Bag(), Bag()]:\n        b.get(1)\n\n\n"
    'def no_counterpart():\n    return "abc".upper()\n'
)


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("builtins") / "lit"
    _write(root, "lib.py", LIB)
    _write(root, "app.py", APP)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}app.{caller}"
    }


@pytest.mark.parametrize(
    "caller",
    [
        "s_join",
        "fs_join",
        "d_get",
        "l_append",
        "dc_get",
        "b_strip",
        "set_update",
        "local_get",
        "local_append",
        "local_strip",
        "App.lookup",
        "App.record",
    ],
)
def test_a_call_on_a_builtin_value_binds_no_first_party_method(
    graph: RecordedGraph, caller: str
) -> None:
    assert not [c for c in _callees(graph, caller) if c.startswith("lib.")]


# Negative: what must not change.


@pytest.mark.parametrize("caller", ["real_bag", "App.fetch", "loop_bags"])
def test_a_first_party_instance_still_binds_exactly(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {"lib.Bag.get": "exact"}


@pytest.mark.parametrize("caller", ["untyped", "loop_keys"])
def test_an_unknown_receiver_keeps_the_name_fallback(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {"lib.Bag.get": "heuristic"}


@pytest.mark.parametrize("caller", ["no_counterpart", "tuple_count", "annotated"])
def test_a_builtin_receiver_that_already_bound_nothing_still_does(
    graph: RecordedGraph, caller: str
) -> None:
    # No first-party `upper`; a parenthesised tuple and an annotated `dict`
    # parameter were already typed.
    assert _callees(graph, caller) == {}
