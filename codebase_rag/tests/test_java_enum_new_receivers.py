"""Java calls on an enum constant or on a `new` expression get their CALLS.

With the default heuristic frontend, `Level.HIGH.weight()` and `new
Box(3).value()` recorded no edge: the receiver `Level.HIGH` named an enum
the same-package lookup skipped and a constant no field lookup knows, and
`new Box(3)` named no variable at all. Both methods were reported as dead
code although the program calls them (issue #2700).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_FILES = {
    "Level.java": """\
package com.acme;

enum Level {
    LOW, HIGH;

    int weight() {
        return ordinal() + 1;
    }
}
""",
    "Box.java": """\
package com.acme;

class Box {
    private final int v;

    Box(int v) {
        this.v = v;
    }

    int value() {
        return v;
    }
}
""",
    "Pair.java": """\
package com.acme;

class Pair<A, B> {
    private final A a;

    Pair(A a, B b) {
        this.a = a;
    }

    A first() {
        return a;
    }
}
""",
    "Request.java": """\
package com.acme;

class Request {
    static class Builder {
        Builder url(String u) {
            return this;
        }
    }
}
""",
    "Op.java": """\
package com.acme;

enum Op {
    ADD {
        int apply(int a, int b) { return a + b; }
    },
    MUL {
        int apply(int a, int b) { return a * b; }
    };

    abstract int apply(int a, int b);
}
""",
    "Calc.java": """\
package com.acme;

class Calc {
    boolean add(String s) {
        return true;
    }

    int weight() {
        return 0;
    }
}
""",
    "Main.java": """\
package com.acme;

import java.util.ArrayList;

public class Main {
    int enumConstant() {
        return Level.HIGH.weight();
    }

    int newExpression() {
        return new Box(3).value();
    }

    int parenthesized() {
        return (new Box(4)).value();
    }

    Integer generic() {
        return new Pair<Integer, String>(1, "x").first();
    }

    Integer diamond() {
        Pair<Integer, String> p = null;
        return new Pair<>(1, "x").first();
    }

    void memberType() {
        new Request.Builder().url("u");
    }

    int constantBody() {
        return Op.ADD.apply(1, 2);
    }

    boolean jdkReceiver() {
        return new ArrayList<String>().add("x");
    }

    int notAConstant() {
        return Level.NONE.weight();
    }
}
""",
}


@pytest.fixture(scope="module")
def calls(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    root = tmp_path_factory.mktemp("java2700") / "jrecv"
    src = root / "src" / "com" / "acme"
    src.mkdir(parents=True)
    for name, text in _FILES.items():
        (src / name).write_text(text, encoding="utf-8")
    mock = MagicMock()
    create_and_run_updater(root, mock, skip_if_missing="java")
    out: dict[str, set[str]] = {}
    for c in get_relationships(mock, cs.RelationshipType.CALLS):
        caller = str(c.args[0][2]).split(".acme.", 1)[-1].split(".", 1)[-1]
        callee = str(c.args[2][2]).split(".acme.", 1)[-1].split(".", 1)[-1]
        out.setdefault(caller, set()).add(callee)
    return out


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("Main.enumConstant()", "Level.weight()"),
        ("Main.newExpression()", "Box.value()"),
        ("Main.parenthesized()", "Box.value()"),
        ("Main.generic()", "Pair.first()"),
        ("Main.diamond()", "Pair.first()"),
        ("Main.memberType()", "Request.Builder.url(String)"),
        ("Main.constantBody()", "Op.apply(int,int)"),
    ],
)
def test_the_receiver_types_the_call(
    calls: dict[str, set[str]], caller: str, callee: str
) -> None:
    assert callee in calls.get(caller, set()), calls


def test_every_constant_body_is_reached(calls: dict[str, set[str]]) -> None:
    # `Op.ADD.apply(...)` runs the constant-specific body; the bodies register
    # as variants of `Op.apply`, all of which the call reaches.
    reached = {c for c in calls.get("Main.constantBody()", set()) if "apply" in c}
    assert len(reached) == 3, reached


def test_other_receivers_take_no_project_edge(calls: dict[str, set[str]]) -> None:
    # Negatives: a JDK type's method is not a project method of the same name,
    # and `Level.NONE` names no constant, so neither binds anywhere.
    assert "Calc.add(String)" not in calls.get("Main.jdkReceiver()", set()), calls
    assert not {c for c in calls.get("Main.notAConstant()", set()) if "weight" in c}, (
        calls
    )
