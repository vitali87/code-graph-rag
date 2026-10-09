"""A Java call on a construction resolves on the constructed type (#3283).

`new Maker().make()` named no variable, so the default frontend sent it down
the unqualified `this.make()` path: inside a class declaring its own `make()`
it bound `exact` to that one, a fake self-call, and otherwise it got no edge,
so a chain built on it (`new Maker().make().go()`, the builder idiom
`new GsonBuilder().setPrettyPrinting().create()`) lost every step. Here the
constructed classes live in another package than their callers.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

_J = "src/main/java/"
_FILES = {
    _J + "other/Maker.java": (
        "package other;\n\npublic class Maker {\n"
        "    public Thing make() {\n        return new Thing();\n    }\n}\n"
    ),
    _J + "other/Thing.java": (
        "package other;\n\npublic class Thing {\n    public void go() {}\n}\n"
    ),
    _J + "other/Builder.java": (
        "package other;\n\npublic class Builder {\n"
        "    public Builder pretty() {\n        return this;\n    }\n\n"
        "    public Printer build() {\n        return new Printer();\n    }\n}\n"
    ),
    _J + "other/Printer.java": (
        "package other;\n\npublic class Printer {\n    public void write() {}\n}\n"
    ),
    # Declares its own `make()`: `new Maker().make()` is never `this.make()`.
    _J + "app/Own.java": (
        "package app;\n\nimport other.Maker;\n\npublic class Own {\n"
        "    Object make() {\n        return null;\n    }\n\n"
        "    void run() {\n        new Maker().make();\n    }\n}\n"
    ),
    _J + "app/Chain.java": (
        "package app;\n\nimport other.Maker;\n\npublic class Chain {\n"
        "    void run() {\n        new Maker().make().go();\n    }\n}\n"
    ),
    _J + "app/Init.java": (
        "package app;\n\nimport other.Builder;\nimport other.Printer;\n\n"
        "public class Init {\n    void run() {\n"
        "        Printer p = new Builder().pretty().build();\n"
        "        p.write();\n    }\n}\n"
    ),
    # Negative: a construction of a type the project does not declare.
    _J + "app/Outside.java": (
        "package app;\n\npublic class Outside {\n"
        "    Object make() {\n        return null;\n    }\n\n"
        "    void run() {\n        new java.util.ArrayList<String>().size();\n"
        '        new StringBuilder().append("a").toString();\n    }\n}\n'
    ),
}


def _short(qn: str) -> str:
    return qn.split("java.", 1)[1] if "java." in qn else qn


@pytest.fixture
def calls(temp_repo: Path, mock_ingestor: MagicMock) -> dict[str, set[tuple[str, str]]]:
    for rel, text in _FILES.items():
        (temp_repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (temp_repo / rel).write_text(text, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor)
    out: dict[str, set[tuple[str, str]]] = {}
    for c in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        props = (
            c.kwargs.get("properties") or (c.args[3] if len(c.args) > 3 else {}) or {}
        )
        out.setdefault(_short(str(c.args[0][2])), set()).add(
            (_short(str(c.args[2][2])), str(props.get(cs.KEY_RESOLUTION)))
        )
    return out


_EXACT = cs.EdgeResolution.EXACT


@pytest.mark.parametrize(
    ("caller", "expected"),
    [
        ("app.Own.Own.run()", {("other.Maker.Maker.make()", _EXACT)}),
        (
            "app.Chain.Chain.run()",
            {("other.Maker.Maker.make()", _EXACT), ("other.Thing.Thing.go()", _EXACT)},
        ),
        (
            "app.Init.Init.run()",
            {
                ("other.Builder.Builder.pretty()", _EXACT),
                ("other.Builder.Builder.build()", _EXACT),
                ("other.Printer.Printer.write()", _EXACT),
            },
        ),
    ],
    ids=["own-same-named-method", "chain-across-files", "builder-in-an-initializer"],
)
def test_a_call_on_a_construction_resolves_on_the_constructed_type(
    calls: dict[str, set[tuple[str, str]]],
    caller: str,
    expected: set[tuple[str, str]],
) -> None:
    assert calls.get(caller, set()) == expected, calls.get(caller)


def test_a_construction_of_an_outside_type_binds_no_own_method(
    calls: dict[str, set[tuple[str, str]]],
) -> None:
    # Negative: `new ArrayList<>().size()` is the JDK's; it must not fall to
    # the unqualified path and bind a first-party method by name.
    assert not {
        callee for callee, _ in calls.get("app.Outside.Outside.run()", set())
    } & {"app.Outside.Outside.make()"}
