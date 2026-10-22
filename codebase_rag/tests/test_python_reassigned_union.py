"""Issue #2842: a local rebound in a branch keeps every type it may hold.

`local_var_types` held one type per name and each assignment overwrote it,
so `s = Circle(); if flag: s = Square(); s._area()` bound only
`Square._area`, as `exact`. `Circle._area`, which runs whenever `flag` is
false, had no caller: dead-code reported it, and `cgr rename` of
`Square._area` rewrote the shared `s._area()` site and broke the Circle path.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SHAPES = """\
from geometry import External


class Circle:
    def _area(self):
        return 3.14159


class Square:
    def _area(self):
        return 4.0


def total(use_square):
    s = Circle()
    if use_square:
        s = Square()
    return s._area()


def either(flag):
    if flag:
        s = Circle()
    else:
        s = Square()
    return s._area()


def guarded():
    try:
        s = Circle()
    except ValueError:
        s = Square()
    return s._area()


def looped(items):
    s = Circle()
    for _ in items:
        s = Square()
    return s._area()


def annotated(s: Circle | Square):
    return s._area()


def straight():
    s = Circle()
    s = Square()
    return s._area()


def reset(flag):
    if flag:
        s = Circle()
    s = Square()
    return s._area()


def single():
    s = Circle()
    return s._area()


def optional(s: Circle | None):
    return s._area()


def same_both(flag):
    if flag:
        s = Circle()
    else:
        s = Circle()
    return s._area()


def make_square() -> Square:
    return Square()


def factory_branch(flag):
    s = Circle()
    if flag:
        s = make_square()
    return s._area()


def alias_branch(flag, other: Square):
    s = Circle()
    if flag:
        s = other
    return s._area()


def factory_then_reset(flag):
    s = Circle()
    if flag:
        s = make_square()
    s = Circle()
    return s._area()


def with_unindexed(s: Circle | External):
    return s._area()
"""

CIRCLE = "shapes.Circle._area"
SQUARE = "shapes.Square._area"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("union") / "shapes"
    _write(root, "shapes.py", SHAPES)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}shapes.{caller}"
    }


@pytest.mark.parametrize(
    "caller",
    [
        "total",
        "either",
        "guarded",
        "looped",
        "annotated",
        "factory_branch",
        "alias_branch",
    ],
)
def test_a_receiver_that_may_hold_either_type_reaches_both_methods(
    graph: RecordedGraph, caller: str
) -> None:
    # One call site, two runtime-possible targets: `overload`, never `exact`.
    callees = _callees(graph, caller)
    assert {k: v for k, v in callees.items() if k in (CIRCLE, SQUARE)} == {
        CIRCLE: "overload",
        SQUARE: "overload",
    }


def test_a_union_with_an_unindexed_member_is_not_exact(graph: RecordedGraph) -> None:
    # `External` comes from a dependency and may define `_area` too: the one
    # first-party target is not the only one this site can run (Greptile,
    # PR #2957).
    callees = _callees(graph, "with_unindexed")
    assert callees.get(CIRCLE) == "overload"


# Negative: what must not change.


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("straight", SQUARE),
        ("reset", SQUARE),
        ("single", CIRCLE),
        ("optional", CIRCLE),
        ("same_both", CIRCLE),
        ("factory_then_reset", CIRCLE),
    ],
    ids=[
        "straight-line-rebinding",
        "unconditional-rebinding-after-a-branch",
        "one-assignment",
        "optional-annotation",
        "same-type-in-both-branches",
        "branch-factory-replaced-after",
    ],
)
def test_a_receiver_of_one_type_still_binds_that_method_exactly(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    callees = _callees(graph, caller)
    assert {k: v for k, v in callees.items() if k in (CIRCLE, SQUARE)} == {
        method: "exact"
    }
