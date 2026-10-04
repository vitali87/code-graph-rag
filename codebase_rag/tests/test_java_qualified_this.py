"""Issue #2936: `Outer.this.m()` calls `m` on the enclosing class.

Inside an inner or anonymous class, `Adapter.this` is the enclosing
`Adapter` instance: the delegate / `nullSafe()` idiom that calls an outer
method the inner class shadows. The receiver resolver only knew a bare
`this`, so `Adapter.this.write(value)` bound nothing (gson: 4 sites).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

ADAPTER = """\
package com.acme;

public abstract class Adapter extends Base {
  public abstract void write(String value);

  public void flush() {}

  public final Adapter nullSafe() {
    return new Adapter() {
      @Override
      public void write(String value) {
        if (value != null) {
          Adapter.this.write(value);
        }
        Adapter.this.flush();
        Adapter.this.close();
      }
    };
  }

  class Inner {
    void flush() {}

    void run() {
      Adapter.this.flush();
      flush();
    }

    class Deeper {
      void go() {
        Inner.this.run();
        Adapter.this.flush();
      }
    }
  }
}
"""

BASE = """\
package com.acme;

public class Base {
  public void close() {}
}
"""

PREFIX = "src.main.java.com.acme"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("jthis") / "jthis"
    _write(root, f"{PREFIX.replace('.', '/')}/Adapter.java", ADAPTER)
    _write(root, f"{PREFIX.replace('.', '/')}/Base.java", BASE)
    return _index(root, MagicMock())


def _callees(graph: RecordedGraph, caller_tail: str) -> set[str]:
    prefix = f"{graph.project}.{PREFIX}."
    return {
        dst.removeprefix(prefix)
        for src, rel, dst, _props in graph.edges
        if rel == "CALLS" and src.removeprefix(prefix).endswith(caller_tail)
    }


@pytest.mark.parametrize(
    ("caller", "callee"),
    [
        ("Inner.run()", "Adapter.Adapter.flush()"),
        ("Inner.Deeper.go()", "Adapter.Adapter.flush()"),
        ("Inner.Deeper.go()", "Adapter.Adapter.Inner.run()"),
        ("Adapter.Adapter.nullSafe.write", "Adapter.Adapter.flush()"),
        ("Adapter.Adapter.nullSafe.write", "Adapter.Adapter.write(String)"),
        ("Adapter.Adapter.nullSafe.write", "Base.Base.close()"),
    ],
    ids=[
        "inner-class",
        "two-levels-out",
        "nested-qualifier",
        "anonymous-class",
        "anonymous-shadowed",
        "inherited",
    ],
)
def test_a_qualified_this_call_binds_the_enclosing_class(
    graph: RecordedGraph, caller: str, callee: str
) -> None:
    assert callee in _callees(graph, caller)


# Negative: what must not change.


def test_an_unqualified_call_in_the_inner_class_binds_its_own_method(
    graph: RecordedGraph,
) -> None:
    # `flush()` in Inner is Inner's own flush, not Adapter's.
    assert "Adapter.Adapter.Inner.flush()" in _callees(graph, "Inner.run()")
