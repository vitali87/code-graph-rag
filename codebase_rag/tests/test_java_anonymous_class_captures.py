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

OTHER = """\
package com.acme;

public class Other {
  public String encode(String s) {
    return s;
  }
}
"""

# Bot review on PR #2991: only what is in scope where the class is created is
# captured, and the method's own locals infer through the captures.
SCOPED = """\
package com.acme;

public class Scoped {
  static Handler sibling() {
    Handler first;
    {
      final Codec codec = new Codec();
      first = new Handler() {
        @Override
        public String handle(String in) {
          return codec.encode(in); // sibling-handler
        }
      };
    }
    {
      final Other codec = new Other();
      codec.encode("y");
    }
    return first;
  }

  static Handler alias(final Codec codec) {
    return new Handler() {
      @Override
      public String handle(String in) {
        var copy = codec;
        return copy.encode(in); // alias-call
      }
    };
  }

  static Handler localField(final Codec codec) {
    class Local implements Handler {
      private final Other codec = new Other();

      @Override
      public String handle(String in) {
        return codec.encode(in); // local-class-field
      }
    }
    return new Local();
  }

  static Handler anonymousField(final Codec codec) {
    return new Handler() {
      private final Other codec = new Other();

      @Override
      public String handle(String in) {
        return codec.encode(in); // anonymous-field
      }
    };
  }
}
"""

# Bot review on PR #2991: the nearest binding of a name wins. A lambda's
# parameter between the method and the class hides the field of that name,
# and a field the class inherits hides a captured local.
BASE = """\
package com.acme;

public class Base {
  protected Other codec = new Other();
}
"""

# Bot review on PR #2991: a private field is not inherited, so it hides no
# captured local in a subclass.
PRIVATE_BASE = """\
package com.acme;

public class PrivateBase {
  private Other codec = new Other();
}
"""

# Bot review on PR #2991: a package-private field is inherited only within
# its package, so in another package it hides no captured local either.
PKG_BASE = """\
package com.acme.other;

import com.acme.Other;

public class PkgBase {
  Other codec = new Other();
}
"""

PKG_SAME_BASE = """\
package com.acme;

public class PkgSameBase {
  Other codec = new Other();
}
"""

NEAREST = """\
package com.acme;

import com.acme.other.PkgBase;
import java.util.function.Function;

public class Nearest {
  private Codec codec = new Codec();

  Function<Other, Handler> typedLambda() {
    return (Other codec) -> {
      class Local implements Handler {
        @Override
        public String handle(String in) {
          return codec.encode(in); // lambda-parameter
        }
      }
      return new Local();
    };
  }

  static Base inherited(final Codec codec) {
    class Local extends Base {
      String run(String in) {
        return codec.encode(in); // inherited-field
      }
    }
    return new Local();
  }

  Handler caught() {
    try {
      return null;
    } catch (Other codec) {
      class Local implements Handler {
        @Override
        public String handle(String in) {
          return codec.encode(in); // catch-parameter
        }
      }
      return new Local();
    }
  }

  Handler withResource() {
    try (Other codec = new Other()) {
      return new Handler() {
        @Override
        public String handle(String in) {
          return codec.encode(in); // resource-variable
        }
      };
    }
  }

  Handler afterResource() {
    try (Other codec = new Other()) {
      return null;
    } finally {
      new Handler() {
        @Override
        public String handle(String in) {
          return codec.encode(in); // resource-out-of-scope
        }
      };
    }
  }

  static PrivateBase privateInherited(final Codec codec) {
    return new PrivateBase() {
      String run(String in) {
        return codec.encode(in); // private-field
      }
    };
  }

  static PkgBase otherPackage(final Codec codec) {
    return new PkgBase() {
      String run(String in) {
        return codec.encode(in); // other-package-field
      }
    };
  }

  static PkgSameBase samePackage(final Codec codec) {
    return new PkgSameBase() {
      String run(String in) {
        return codec.encode(in); // same-package-field
      }
    };
  }

  static Base anonymousInherited(final Codec codec) {
    return new Base() {
      String run(String in) {
        return codec.encode(in); // anonymous-inherited-field
      }
    };
  }
}
"""

# Bot review on PR #2991: a test source tree holds the same package as the
# main tree, so the package a file declares, not its folder, decides whether
# a package-private field is inherited.
SPLIT_TREE = """\
package com.acme;

import com.acme.PkgSameBase;

public class SplitTree {
  static PkgSameBase sameDeclaredPackage(final Codec codec) {
    return new PkgSameBase() {
      String run(String in) {
        return codec.encode(in); // test-tree-same-package
      }
    };
  }
}
"""

PREFIX = "src.main.java.com.acme"
TEST_PREFIX = "src.test.java.com.acme"
OTHER_ENCODE = "Other.Other.encode(String)"


def _scoped_line(marker: str, source: str = SCOPED) -> int:
    return next(
        number
        for number, text in enumerate(source.splitlines(), start=1)
        if marker in text
    )


ENCODE = "Codec.Codec.encode(String)"


@pytest.fixture(scope="module")
def graph(tmp_path_factory: pytest.TempPathFactory) -> RecordedGraph:
    root = tmp_path_factory.mktemp("jcapture") / "jcapture"
    base = PREFIX.replace(".", "/")
    _write(root, f"{base}/Codec.java", CODEC)
    _write(root, f"{base}/Handler.java", HANDLER)
    _write(root, f"{base}/Factory.java", FACTORY)
    _write(root, f"{base}/Other.java", OTHER)
    _write(root, f"{base}/Scoped.java", SCOPED)
    _write(root, f"{base}/Base.java", BASE)
    _write(root, f"{base}/PrivateBase.java", PRIVATE_BASE)
    _write(root, f"{base}/other/PkgBase.java", PKG_BASE)
    _write(root, f"{base}/PkgSameBase.java", PKG_SAME_BASE)
    _write(root, f"{base}/Nearest.java", NEAREST)
    _write(root, f"{TEST_PREFIX.replace('.', '/')}/SplitTree.java", SPLIT_TREE)
    return _index(root, MagicMock())


def _callers_of_encode(
    graph: RecordedGraph,
    target: str = ENCODE,
    source: str = "Factory",
    source_root: str = PREFIX,
) -> dict[int, str]:
    prefix = f"{graph.project}.{PREFIX}."
    return {
        int(props["line"]): str(props.get("resolution"))
        for src, rel, dst, props in graph.edges
        if rel == "CALLS"
        and dst == f"{prefix}{target}"
        and src.startswith(f"{graph.project}.{source_root}.{source}.")
        and props.get("line")
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


def test_a_same_named_local_of_a_sibling_block_is_not_captured(
    graph: RecordedGraph,
) -> None:
    # The handler is created in the block declaring `Codec codec`; the
    # `Other codec` of the next block is another variable.
    line = _scoped_line("sibling-handler")
    assert _callers_of_encode(graph, source="Scoped").get(line) == "exact"
    assert line not in _callers_of_encode(graph, OTHER_ENCODE, "Scoped")


def test_a_local_assigned_from_a_capture_is_typed(graph: RecordedGraph) -> None:
    line = _scoped_line("alias-call")
    assert _callers_of_encode(graph, source="Scoped").get(line) == "exact"


def test_a_field_of_the_local_class_shadows_the_captured_name(
    graph: RecordedGraph,
) -> None:
    # Bot review on PR #2991: in Java the class's own `Other codec` field, not
    # the captured `Codec codec`, is what `codec` names inside its methods.
    line = _scoped_line("local-class-field")
    assert _callers_of_encode(graph, OTHER_ENCODE, "Scoped").get(line) == "exact"
    assert line not in _callers_of_encode(graph, source="Scoped")


def test_a_field_of_the_anonymous_class_shadows_the_captured_name(
    graph: RecordedGraph,
) -> None:
    line = _scoped_line("anonymous-field")
    assert line not in _callers_of_encode(graph, source="Scoped")


@pytest.mark.parametrize(
    "marker",
    [
        "lambda-parameter",
        "inherited-field",
        "anonymous-inherited-field",
        "catch-parameter",
        "resource-variable",
    ],
)
def test_the_nearest_binding_of_the_name_types_the_receiver(
    graph: RecordedGraph, marker: str
) -> None:
    line = _scoped_line(marker, NEAREST)
    assert _callers_of_encode(graph, OTHER_ENCODE, "Nearest").get(line) == "exact"
    assert line not in _callers_of_encode(graph, source="Nearest")


def test_a_private_superclass_field_does_not_hide_the_capture(
    graph: RecordedGraph,
) -> None:
    line = _scoped_line("private-field", NEAREST)
    assert _callers_of_encode(graph, source="Nearest").get(line) == "exact"
    assert line not in _callers_of_encode(graph, OTHER_ENCODE, "Nearest")


def test_a_package_private_field_of_another_package_does_not_hide_the_capture(
    graph: RecordedGraph,
) -> None:
    line = _scoped_line("other-package-field", NEAREST)
    assert _callers_of_encode(graph, source="Nearest").get(line) == "exact"
    assert line not in _callers_of_encode(graph, OTHER_ENCODE, "Nearest")


def test_a_package_private_field_of_the_same_package_hides_the_capture(
    graph: RecordedGraph,
) -> None:
    line = _scoped_line("same-package-field", NEAREST)
    assert _callers_of_encode(graph, OTHER_ENCODE, "Nearest").get(line) == "exact"
    assert line not in _callers_of_encode(graph, source="Nearest")


def test_a_package_private_field_of_the_same_declared_package_hides_the_capture(
    graph: RecordedGraph,
) -> None:
    # SplitTree sits under src/test/java and PkgSameBase under src/main/java,
    # both declaring `package com.acme`: the field is inherited.
    line = _scoped_line("test-tree-same-package", SPLIT_TREE)
    callers = _callers_of_encode(graph, OTHER_ENCODE, "SplitTree", TEST_PREFIX)
    assert callers.get(line) == "exact"
    assert line not in _callers_of_encode(
        graph, source="SplitTree", source_root=TEST_PREFIX
    )


def test_a_resource_is_not_captured_in_its_finally_block(
    graph: RecordedGraph,
) -> None:
    # Bot review on PR #2991: in `finally` the resource is out of scope, so
    # `codec` there is the class's `Codec codec` field.
    line = _scoped_line("resource-out-of-scope", NEAREST)
    assert _callers_of_encode(graph, source="Nearest").get(line) == "exact"
    assert line not in _callers_of_encode(graph, OTHER_ENCODE, "Nearest")


# Negative: what must not change.


@pytest.mark.parametrize("line", [60, 64], ids=["lambda", "direct"])
def test_a_lambda_or_direct_call_still_binds(graph: RecordedGraph, line: int) -> None:
    assert _callers_of_encode(graph).get(line) == "exact"


def test_an_inner_parameter_shadows_the_captured_one(graph: RecordedGraph) -> None:
    # `handle(String codec)` hides the outer `codec`: `codec.trim()` is a
    # String call, never Codec's.
    assert 54 not in _callers_of_encode(graph)
