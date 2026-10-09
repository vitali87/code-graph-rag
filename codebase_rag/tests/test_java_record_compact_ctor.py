"""A Java record's compact constructor is the canonical constructor.

`public Range { Checks.ordered(lo, hi); }` was not indexed at all: its calls
were credited to the file's Module, and with no `Range(int,int)` node, `new
Range(5, 1)` bound `heuristic` to the only constructor left, `Range(int)`,
which never runs for two arguments. `this(only, only)` reached nothing
(issue #2703).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)

_RANGE = """\
package com.acme;

public record Range(int lo, int hi) {
    public Range {
        Checks.ordered(lo, hi);
    }

    public Range(int only) {
        this(only, only);
    }

    public int width() {
        return hi - lo;
    }
}
"""
_CHECKS = """\
package com.acme;

final class Checks {
    private Checks() {}

    static void ordered(int a, int b) {
        if (a > b) throw new IllegalArgumentException("lo > hi");
    }
}
"""
_RANGE_TEST = """\
package com.acme;

class RangeTest {
    void rejectsReversed() {
        new Range(5, 1);
    }

    void single() {
        new Range(3);
    }
}
"""
# No canonical constructor written: the compiler's implicit one has no node.
_POINT = """\
package com.acme;

record Point(int x, int y) {
    Point(int x) {
        this(x, 0);
    }
}
"""
_SHAPES = """\
package com.acme;

class Varargs {
    Varargs(int... xs) {}
}

class Base {
    Base(int a) {}
}

class Child extends Base {
    Child() {
        super(1);
    }

    Child(String s) {
        this();
    }
}

class Use {
    void pointPair() {
        new Point(1, 2);
    }

    void pointOne() {
        new Point(3);
    }

    void varargs() {
        new Varargs(1, 2, 3);
    }
}
"""

_Calls = dict[tuple[str, str], str]


@pytest.fixture(scope="module")
def indexed(tmp_path_factory: pytest.TempPathFactory) -> MagicMock:
    root = tmp_path_factory.mktemp("java2703") / "jrec"
    src = root / "src" / "com" / "acme"
    src.mkdir(parents=True)
    for name, text in (
        ("Range.java", _RANGE),
        ("Checks.java", _CHECKS),
        ("RangeTest.java", _RANGE_TEST),
        ("Point.java", _POINT),
        ("Shapes.java", _SHAPES),
    ):
        (src / name).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="java")
    return mock


def _short(qn: str) -> str:
    # `jrec.src.com.acme.Range.Range.Range(int,int)` -> `Range.Range(int,int)`
    return qn.split(".acme.", 1)[-1].split(".", 1)[-1]


@pytest.fixture(scope="module")
def calls(indexed: MagicMock) -> _Calls:
    out: _Calls = {}
    for c in get_relationships(indexed, cs.RelationshipType.CALLS):
        props = c.kwargs.get("properties") or {}
        out[(_short(str(c.args[0][2])), _short(str(c.args[2][2])))] = str(
            props.get(cs.KEY_RESOLUTION)
        )
    return out


def test_the_compact_constructor_is_the_canonical_constructor(
    indexed: MagicMock,
) -> None:
    methods = {
        _short(str(c.args[1][cs.KEY_QUALIFIED_NAME]))
        for c in get_nodes(indexed, cs.NodeLabel.METHOD)
    }
    assert "Range.Range(int,int)" in methods, methods
    assert "Range.Range(int)" in methods, methods


def test_its_calls_belong_to_it(calls: _Calls) -> None:
    assert calls.get(("Range.Range(int,int)", "Checks.ordered(int,int)")) == "exact"
    assert not [
        k for k in calls if k[1] == "Checks.ordered(int,int)" and "(" not in k[0]
    ]


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("RangeTest.rejectsReversed()", "Range.Range(int,int)"),
        ("Range.Range(int)", "Range.Range(int,int)"),
        ("RangeTest.single()", "Range.Range(int)"),
        ("Point.Point(int)", None),
        ("Use.pointPair()", None),
        ("Use.pointOne()", "Point.Point(int)"),
        ("Use.varargs()", "Varargs.Varargs(int...)"),
        ("Child.Child(String)", "Child.Child()"),
    ],
    ids=[
        "new-two-args",
        "this-delegation",
        "new-one-arg",
        "this-to-implicit-canonical",
        "new-to-implicit-canonical",
        "new-matching-arity-only",
        "varargs",
        "this-in-a-class",
    ],
)
def test_a_construction_reaches_the_constructor_its_arity_selects(
    calls: _Calls, caller: str, callee: str | None
) -> None:
    targets = {to: res for (src, to), res in calls.items() if src == caller}
    ctor_targets = {to: res for to, res in targets.items() if "(" in to}
    if callee is None:
        assert ctor_targets == {}, targets
    else:
        assert ctor_targets == {callee: "exact"}, targets


def test_super_is_not_a_sibling_delegation(calls: _Calls) -> None:
    # Negative: `super(1)` runs the superclass's constructor, not one of the
    # class's own, so the `this(...)` pass gives it no edge.
    assert not [to for (src, to) in calls if src == "Child.Child()" and "(" in to]
