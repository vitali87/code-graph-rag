"""Issue #2707: C, C++ and Scala public API is a dead-code root.

Every function and method of these languages was stored `is_exported:
false` (C++ counted only a C++20 module `export`), so `dead-code` listed a
library's whole public surface and every private helper it reaches. The
rules are each language's own: C external linkage (no `static`), a C++
non-`static` namespace function or a non-`private` class member, a Scala
definition that is not `private`.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.tests.test_is_exported_roots import _one, _run
from codebase_rag.tests.test_rename_op import RecordedGraph, _index, _write
from codebase_rag.types_defs import PropertyParams, ResultRow

# The issue's repro: a library, so nothing in it is dead.
MATHLIB_C = """\
static int square(int x) { return x * x; }

int sum_of_squares(int a, int b) { return square(a) + square(b); }
"""
SHAPES_CPP = """\
namespace geo {
class Circle {
public:
    explicit Circle(double r) : r_(r) {}
    double area() const { return 3.14 * r_ * r_; }
private:
    double r_;
};
double total_area(const Circle& a, const Circle& b) { return a.area() + b.area(); }
}
"""
GEO_SCALA = """\
package geo

object Geo {
  private def sq(x: Double): Double = x * x
  def dist(a: Double, b: Double): Double = math.sqrt(sq(a) + sq(b))
}
"""

# Every visibility each rule must tell apart.
UTIL_C = """\
static inline int clamp(int x) { return x; }

extern int scale(int x) { return clamp(x) * 2; }
"""
KINDS_CPP = """\
static int file_local() { return 1; }

namespace {
int anonymous_local() { return 2; }
}

class Box {
    int hidden() { return 3; }
public:
    int shown() { return hidden(); }
protected:
    int for_subclasses() { return 4; }
private:
    int secret() { return 5; }
};

struct Point {
    int x() { return 6; }
};

int make() {
    struct Local {
        int value() { return 7; }
    };
    return Local().value() + file_local() + anonymous_local();
}
"""
KINDS_SCALA = """\
package kinds

class Account {
  protected def audit(): Int = 1
  private[kinds] def internal(): Int = 2
  def balance(): Int = {
    def round(x: Int): Int = x
    round(audit() + internal())
  }
}

def topLevel(): Int = 3

private def hiddenTop(): Int = 4
"""


@pytest.fixture
def exported(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {
            "mathlib.c": MATHLIB_C,
            "util.c": UTIL_C,
            "shapes.cpp": SHAPES_CPP,
            "kinds.cpp": KINDS_CPP,
            "Geo.scala": GEO_SCALA,
            "Kinds.scala": KINDS_SCALA,
        },
    )


@pytest.mark.parametrize(
    "symbol",
    [
        "mathlib.sum_of_squares",
        "util.scale",
        "shapes.geo.Circle.Circle",
        "shapes.geo.Circle.area",
        "shapes.geo.total_area",
        "kinds.make",
        "kinds.Box.shown",
        "kinds.Box.for_subclasses",
        "kinds.Point.x",
        "Geo.Geo.dist",
        "Kinds.Account.audit",
        "Kinds.Account.balance",
        "Kinds.topLevel",
    ],
)
def test_public_api_is_exported(exported: dict[str, bool], symbol: str) -> None:
    assert _one(exported, f".{symbol}") is True


# Negative: what must not change.


@pytest.mark.parametrize(
    "symbol",
    [
        "mathlib.square",
        "util.clamp",
        "kinds.file_local",
        "kinds.anonymous_local",
        "kinds.Box.hidden",
        "kinds.Box.secret",
        "kinds.Local.value",
        "Geo.Geo.sq",
        "Kinds.Account.internal",
        "Kinds.Account.round",
        "Kinds.hiddenTop",
    ],
)
def test_what_only_its_own_file_or_class_can_call_stays_private(
    exported: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn, flag in exported.items() if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert not any(exported[qn] for qn in matches), symbol


class _Graph:
    """The dead-code queries answered from what the indexer emitted."""

    def __init__(self, graph: RecordedGraph) -> None:
        self._nodes = [
            {
                "label": props[cs.KEY_LABEL],
                "qualified_name": qn,
                "name": props.get(cs.KEY_NAME),
                "path": props.get(cs.KEY_PATH),
                "start_line": props.get(cs.KEY_START_LINE),
                "end_line": props.get(cs.KEY_END_LINE),
                "decorators": props.get(cs.KEY_DECORATORS, []),
                "is_exported": props.get(cs.KEY_IS_EXPORTED, False),
                "overrides_external": props.get(cs.KEY_OVERRIDES_EXTERNAL, False),
            }
            for qn, props in graph.nodes.items()
            if props[cs.KEY_LABEL]
            in (cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value)
        ]
        labels = {qn: props[cs.KEY_LABEL] for qn, props in graph.nodes.items()}
        self._rels = [
            {
                "from_label": labels.get(src),
                "from_qn": src,
                "rel_type": rel,
                "to_label": labels.get(dst),
                "to_qn": dst,
            }
            for src, rel, dst, _props in graph.edges
        ]

    def fetch_all(
        self, query: str, params: PropertyParams | None = None
    ) -> list[ResultRow]:
        return self._nodes if query == cq.CYPHER_DEAD_CODE_NODES else self._rels


def _indexed(tmp_path: Path, files: dict[str, str]) -> RecordedGraph:
    root = tmp_path / "apiroots"
    for rel, text in files.items():
        _write(root, rel, text)
    return _index(root, MagicMock())


def test_the_issue_library_has_no_dead_code(tmp_path: Path) -> None:
    graph = _indexed(
        tmp_path,
        {"mathlib.c": MATHLIB_C, "shapes.cpp": SHAPES_CPP, "Geo.scala": GEO_SCALA},
    )
    config = default_dead_code_config(include_tests=True, include_classes=False)

    assert collect_dead_code(_Graph(graph), graph.project, config) == []


def test_a_cpp_module_export_edge_is_still_only_for_export(tmp_path: Path) -> None:
    # `is_exported` widens to public API; the Module -[EXPORTS]-> edge stays
    # the C++20 `export` it always meant.
    graph = _indexed(tmp_path, {"shapes.cpp": SHAPES_CPP, "kinds.cpp": KINDS_CPP})

    assert [e for e in graph.edges if e[1] == cs.RelationshipType.EXPORTS.value] == []


# Bot review on PR #2952: shapes the first rules misread.
LINKAGE_C = """\
static int helper(void);

int helper(void) { return 1; }

int api(void) { return helper(); }

#ifdef FEATURE
int guarded(void) { return 2; }
#endif
"""
EXTRA_CPP = """\
struct Factory {
    static int create() { return 1; }
private:
    static int hidden_static() { return 2; }
};

template <typename T> T convert(T value) { return value; }

namespace lib {
template <typename T> T twice(T value) { return value; }
}

#ifdef FEATURE
int guarded_cpp() { return 3; }
#endif

class Guarded {
public:
#ifdef FEATURE
    int maybe() { return 4; }
#endif
};

namespace {
template <typename T> T anon_tmpl(T v) { return v; }
}
"""
LOCAL_SCALA = """\
package loc

object Holder {
  private def unused(): Int = {
    object Local { def helper(): Int = 1 }
    Local.helper()
  }
}
"""


@pytest.fixture
def reviewed(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {"linkage.c": LINKAGE_C, "extra.cpp": EXTRA_CPP, "Local.scala": LOCAL_SCALA},
    )


@pytest.mark.parametrize(
    "symbol",
    [
        "linkage.api",
        "linkage.guarded",
        "extra.Factory.create",
        "extra.convert",
        "extra.lib.twice",
        "extra.guarded_cpp",
        "extra.Guarded.maybe",
    ],
    ids=[
        "c-function",
        "c-guarded-function",
        "cpp-static-member",
        "cpp-function-template",
        "cpp-namespace-template",
        "cpp-guarded-function",
        "cpp-guarded-member",
    ],
)
def test_reviewed_public_api_is_exported(
    reviewed: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in reviewed if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert all(reviewed[qn] for qn in matches), symbol


@pytest.mark.parametrize(
    "symbol",
    ["linkage.helper", "extra.Factory.hidden_static", "anon_tmpl", "Local.helper"],
    ids=[
        "c-static-by-earlier-declaration",
        "cpp-private-static-member",
        "cpp-anonymous-namespace-template",
        "scala-object-local-to-a-def",
    ],
)
def test_reviewed_private_code_stays_private(
    reviewed: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in reviewed if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert not any(reviewed[qn] for qn in matches), symbol
