"""Issue #2937: an anonymous or local class's method sees the enclosing
method's parameters and locals.

A method declared in an anonymous class body (the pre-lambda closure:
adapters, listeners, comparators) calls what it captures from the method
that creates it, `codec.encode(in)` with `codec` a parameter of that method.
Its type map held only its own scope, so the receiver was untyped and the
call bound nothing, while the same call from a lambda bound exactly (gson:
28 sites).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write

CODEC = """\
package com.acme;

public class Codec {
  public String encode(String s) {
    return s;
  }
}
"""

HANDLER = """\
package com.acme;

public interface Handler {
  String handle(String in);
}
"""

FACTORY = """\
package com.acme;

public class Factory {
  private Codec field = new Codec();

  static Handler fromParam(final Codec codec) {
    return new Handler() {
      @Override
      public String handle(String in) {
        return codec.encode(in);
      }
    };
  }

  static Handler fromLocal() {
    final Codec local = new Codec();
    return new Handler() {
      @Override
      public String handle(String in) {
        return local.encode(in);
      }
    };
  }

  static Handler nested(final Codec codec) {
    return new Handler() {
      @Override
      public String handle(String in) {
        Handler inner = new Handler() {
          @Override
          public String handle(String again) {
            return codec.encode(again);
          }
        };
        return inner.handle(in);
      }
    };
  }

  static Handler localClass(final Codec codec) {
    class Local implements Handler {
      @Override
      public String handle(String in) {
        return codec.encode(in);
      }
    }
    return new Local();
  }

  static Handler shadowed(final Codec codec) {
    return new Handler() {
      @Override
      public String handle(String codec) {
        return codec.trim();
      }
    };
  }

  static Handler lambdaFromParam(Codec codec) {
    return in -> codec.encode(in);
  }

  String direct(Codec codec) {
    return codec.encode("x");
  }
}
"""

PREFIX = "src.main.java.com.acme"
ENCODE = "Codec.Codec.encode(String)"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("jcapture") / "jcapture"
    base = PREFIX.replace(".", "/")
    _write(root, f"{base}/Codec.java", CODEC)
    _write(root, f"{base}/Handler.java", HANDLER)
    _write(root, f"{base}/Factory.java", FACTORY)
    return _index(root, MagicMock())


def _callers_of_encode(graph: RecordedGraph) -> dict[int, str]:
    prefix = f"{graph.project}.{PREFIX}."
    return {
        int(props["line"]): str(props.get("resolution"))
        for _src, rel, dst, props in graph.edges
        if rel == "CALLS" and dst == f"{prefix}{ENCODE}" and props.get("line")
    }


@pytest.mark.parametrize(
    "line",
    [10, 20, 32, 44],
    ids=["captured-parameter", "captured-local", "two-anonymous-levels", "local-class"],
)
def test_a_captured_variable_types_the_receiver(
    graph: RecordedGraph, line: int
) -> None:
    assert _callers_of_encode(graph).get(line) == "exact"


# Negative: what must not change.


@pytest.mark.parametrize("line", [60, 64], ids=["lambda", "direct"])
def test_a_lambda_or_direct_call_still_binds(graph: RecordedGraph, line: int) -> None:
    assert _callers_of_encode(graph).get(line) == "exact"


def test_an_inner_parameter_shadows_the_captured_one(graph: RecordedGraph) -> None:
    # `handle(String codec)` hides the outer `codec`: `codec.trim()` is a
    # String call, never Codec's.
    assert 54 not in _callers_of_encode(graph)
