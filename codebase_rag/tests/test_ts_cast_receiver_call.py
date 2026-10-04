"""Issue #2890: a TS method call on a cast receiver binds the cast's type.

`(s as Service).handle()` and `(<Service>s).handle()` name the receiver's
type at the call site, yet the call name kept the cast's text, which the
resolver cannot read, so the call bound nothing: a method reached only
through a cast was reported dead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SOURCE = """\
class Service { handle(x: number): number { return x + 1; } }
class Box<T> { get(): number { return 1; } }
class Other { other(): number { return 2; } }

function plain(s: Service) { return s.handle(1); }
function casted(s: unknown) { return (s as Service).handle(2); }
function angleCast(s: unknown) { return (<Service>s).handle(3); }
function doubleCast(s: string) { return (s as unknown as Service).handle(4); }
function nestedParens(s: unknown) { return ((s as Service)).handle(5); }
function assertedCast(s: unknown) { return (s as Service)!.handle(6); }
function genericCast(b: unknown) { return (b as Box<number>).get(); }
function anyCast(s: unknown) { return (s as any).handle(7); }
function otherCast(s: unknown) { return (s as Other).other(); }
"""

# Two classes sharing `run`, and a local VALUE named like the cast's type:
# TypeScript keeps types and values apart, so `as Real` still names the
# class Real (Greptile, PR #2959).
SHADOW = """\
class Real { run(): number { return 1; } }
class Fake { run(): number { return 2; } }

function shadowedCast(s: unknown) {
  const Real = new Fake();
  return (s as Real).run();
}
"""

HANDLE = "m.Service.handle"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("tscast") / "tscast"
    _write(root, "m.ts", SOURCE)
    _write(root, "shadow.ts", SHADOW)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller: str) -> dict[str, str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}m.{caller}"
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("casted", HANDLE),
        ("angleCast", HANDLE),
        ("doubleCast", HANDLE),
        ("nestedParens", HANDLE),
        ("assertedCast", HANDLE),
        ("genericCast", "m.Box.get"),
        ("otherCast", "m.Other.other"),
    ],
)
def test_a_cast_receiver_binds_the_method_of_the_cast_type(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert _callees(graph, caller) == {callee: "exact"}


def test_a_local_value_named_like_the_cast_type_does_not_redirect_it(
    graph: RecordedGraph,
) -> None:
    prefix = f"{graph.project}."
    callees = {
        dst.removeprefix(prefix): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS" and src == f"{prefix}shadow.shadowedCast"
    }
    assert callees == {"shadow.Real.run": "exact"}


# Negative: what must not change.


def test_a_plain_typed_receiver_still_binds(graph: RecordedGraph) -> None:
    assert _callees(graph, "plain") == {HANDLE: "exact"}


def test_a_cast_to_any_still_names_no_type(graph: RecordedGraph) -> None:
    assert HANDLE not in _callees(graph, "anyCast")
