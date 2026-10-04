"""Issue #2891: a Go call on a type-assertion receiver binds the asserted type.

`a.(Dog).Fetch()` names the concrete type at the call site, and hoisting it
(`d := a.(Dog); d.Fetch()`) already resolved, yet the direct form bound
nothing: a method reached only through an assertion was reported dead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SOURCE = """\
package zoo

type Animal interface { Sound() string }

type Dog struct{}

func (d Dog) Sound() string { return "woof" }
func (d Dog) Fetch() string { return "fetching" }

type Cat struct{}

func (c *Cat) Purr() string { return "purr" }

func plainCall(d Dog) string { return d.Fetch() }
func assertCall(a Animal) string { return a.(Dog).Fetch() }
func pointerAssert(x any) string { return x.(*Cat).Purr() }
func parenAssert(a Animal) string { return (a.(Dog)).Fetch() }
func hoisted(a Animal) string {
	d := a.(Dog)
	return d.Fetch()
}
"""


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("goassert") / "goassert"
    _write(root, "go.mod", "module example.com/goassert\n\ngo 1.21\n")
    _write(root, "zoo/zoo.go", SOURCE)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}.zoo.zoo."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("assertCall", "Dog.Fetch"),
        ("pointerAssert", "Cat.Purr"),
        ("parenAssert", "Dog.Fetch"),
    ],
)
def test_a_type_assertion_receiver_binds_the_asserted_type(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    assert _callees(graph, caller) == {method: "exact"}


# Negative: what must not change.


@pytest.mark.parametrize("caller", ["plainCall", "hoisted"])
def test_a_typed_or_hoisted_receiver_still_binds(
    graph: RecordedGraph, caller: str
) -> None:
    assert _callees(graph, caller) == {"Dog.Fetch": "exact"}
