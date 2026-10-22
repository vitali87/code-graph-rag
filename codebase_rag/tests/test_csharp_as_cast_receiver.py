"""Issue #2892: a C# call on an `as`-cast receiver binds the cast's type.

`((Service)o).Handle()` typed its receiver from the cast, but the equivalent
safe cast `(o as Service).Handle()` was not unwrapped, so it bound nothing:
a method reached only through an `as` cast was reported dead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

SOURCE = """\
namespace App {
  public class Service { public int Handle(int x) { return x + 1; } }
  public class Box<T> { public int Get() { return 1; } }
  public static class Extensions { public static int Twice(this Service s) { return 2; } }
  public class Program {
    public int PlainCall(Service s) { return s.Handle(1); }
    public int CastCall(object o) { return ((Service)o).Handle(2); }
    public int AsCall(object o) { return (o as Service).Handle(3); }
    public int AsNullForgiving(object o) { return (o as Service)!.Handle(4); }
    public int AsGeneric(object o) { return (o as Box<int>).Get(); }
    public int AsExtension(object o) { return (o as Service).Twice(); }
  }
}
"""

HANDLE = "m.App.Service.Handle"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("csas") / "csas"
    _write(root, "m.cs", SOURCE)
    return _index(root, MagicMock())


def _binds(callees: set[str], method: str) -> bool:
    # A C# method qn carries its parameter list unless its class is generic.
    return any(c == method or c.startswith(f"{method}(") for c in callees)


def _callees(graph: RecordedGraph, caller: str) -> set[str]:
    prefix = f"{graph.project}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == "CALLS"
        and src.removeprefix(prefix).startswith(f"m.App.Program.{caller}(")
    }


@pytest.mark.parametrize(
    ("caller", "method"),
    [
        ("AsCall", HANDLE),
        ("AsNullForgiving", HANDLE),
        ("AsGeneric", "m.App.Box.Get"),
        ("AsExtension", "m.App.Extensions.Twice"),
    ],
)
def test_an_as_cast_receiver_binds_the_cast_type(
    graph: RecordedGraph, caller: str, method: str
) -> None:
    assert _binds(_callees(graph, caller), method)


# Negative: what must not change.


@pytest.mark.parametrize("caller", ["PlainCall", "CastCall"])
def test_a_typed_or_c_style_cast_receiver_still_binds(
    graph: RecordedGraph, caller: str
) -> None:
    assert _binds(_callees(graph, caller), HANDLE)
