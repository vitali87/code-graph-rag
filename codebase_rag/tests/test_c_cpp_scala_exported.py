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
        "kinds.make.Local.value",
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
OWNER_SCALA = """\
package own

object Outer {
  private object Hidden { def helper(): Int = 1 }
  private class Inner { def work(): Int = 2 }
  object Open { def shown(): Int = 3 }
}
"""


@pytest.fixture
def reviewed(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {
            "linkage.c": LINKAGE_C,
            "extra.cpp": EXTRA_CPP,
            "Local.scala": LOCAL_SCALA,
            "Owner.scala": OWNER_SCALA,
        },
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
        "Outer.Open.shown",
    ],
    ids=[
        "c-function",
        "c-guarded-function",
        "cpp-static-member",
        "cpp-function-template",
        "cpp-namespace-template",
        "cpp-guarded-function",
        "cpp-guarded-member",
        "scala-member-of-a-public-nested-object",
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
    [
        "linkage.helper",
        "extra.Factory.hidden_static",
        "anon_tmpl",
        "Local.helper",
        "Outer.Hidden.helper",
        "Outer.Inner.work",
    ],
    ids=[
        "c-static-by-earlier-declaration",
        "cpp-private-static-member",
        "cpp-anonymous-namespace-template",
        "scala-object-local-to-a-def",
        "scala-member-of-a-private-object",
        "scala-member-of-a-private-class",
    ],
)
def test_reviewed_private_code_stays_private(
    reviewed: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in reviewed if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert not any(reviewed[qn] for qn in matches), symbol


# Bot review on PR #2952: a `static` declaration gives internal linkage to
# the definition in its own namespace with its own parameters, and a
# definition qualified by a namespace is a namespace function.
LINKAGE_CPP = """\
namespace N {
static void helper();
void helper() {}
void visible() {}
}

static void process(int);
void process(double) {}
void process(int) {}

namespace Q {
namespace V {
void f();
}
void V::f() {}
}

struct Box {
    int area();
};
int Box::area() { return 1; }

namespace M {
static void hidden();
}
void M::hidden() {}
"""


@pytest.fixture
def linkage(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"link2.cpp": LINKAGE_CPP})


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("link2.N.helper@3", False),
        ("link2.process@8", True),
        ("link2.V.f", True),
        ("link2.M.hidden", False),
    ],
    ids=[
        "static-declared-in-its-namespace",
        "another-overload-than-the-static-one",
        "a-function-qualified-by-its-namespace",
        "static-declared-in-the-qualifying-namespace",
    ],
)
def test_cpp_linkage_follows_namespace_and_signature(
    linkage: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(linkage, f".{symbol}") is is_exported


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("link2.N.visible", True),
        ("link2.process@9", False),
        ("link2.Box.area", False),
    ],
    ids=[
        "an-unrelated-namespace-function",
        "the-overload-declared-static",
        "an-out-of-line-member-is-left-alone",
    ],
)
def test_cpp_linkage_that_must_not_change(
    linkage: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(linkage, f".{symbol}") is is_exported


# Greptile on PR #2952: a member is only as visible as every class around
# it, and a C nested function has no linkage.
NESTED_CPP = """\
class Outer {
private:
    struct Hidden {
        static int helper() { return 1; }
        struct Below { void below_hidden() {} };
    };
    struct Fwd;
    template <class T> struct Tmpl { void tmpl_hidden() {} };
public:
    class Open {
        struct Deep { void deep_default() {} };
    public:
        struct Inner { void inner_public() {} };
    private:
        class Closed { public: void closed_public() {} };
    };
protected:
    struct Prot { void prot_public() {} };
};

class Plain { struct ByDefault { void by_default() {} }; };
struct Pub { struct Nested { void nested_public() {} }; };

struct Outer::Fwd { void fwd_member() {} };

namespace ns {
namespace { struct Anon { void anon_member() {} }; }
class Box { int qualified_member(); };
namespace in { void qualified_free(); }
}
int ns::Box::qualified_member() { return 2; }
void ns::in::qualified_free() {}
"""
NESTED_C = """\
int outer(void) {
    int nested(int x) { return x; }
    return nested(1);
}
"""


@pytest.fixture
def nested(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"nest.cpp": NESTED_CPP, "gnu.c": NESTED_C})


@pytest.mark.parametrize(
    "symbol",
    [
        "Hidden.helper",
        "Below.below_hidden",
        "Tmpl.tmpl_hidden",
        "Deep.deep_default",
        "Closed.closed_public",
        "ByDefault.by_default",
        "fwd_member",
        "Anon.anon_member",
        "Box.qualified_member",
        "gnu.nested",
    ],
    ids=[
        "cpp-member-of-a-private-nested-struct",
        "cpp-public-class-inside-a-private-one",
        "cpp-member-of-a-private-nested-template",
        "cpp-member-of-a-default-private-nested-struct",
        "cpp-member-of-a-private-class-two-levels-down",
        "cpp-nested-struct-of-a-class-before-any-label",
        "cpp-member-of-a-nested-class-defined-out-of-line",
        "cpp-member-of-a-class-in-an-anonymous-namespace",
        "cpp-out-of-line-member-qualified-through-a-namespace",
        "c-gnu-nested-function",
    ],
)
def test_a_member_of_a_hidden_class_stays_private(
    nested: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in nested if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert not any(nested[qn] for qn in matches), symbol


@pytest.mark.parametrize(
    "symbol",
    [
        "Inner.inner_public",
        "Prot.prot_public",
        "Nested.nested_public",
        "in.qualified_free",
        "gnu.outer",
    ],
    ids=[
        "cpp-public-nested-class-of-a-public-class",
        "cpp-protected-nested-struct-subclasses-reach",
        "cpp-struct-nested-in-a-struct",
        "cpp-function-qualified-by-two-namespaces",
        "c-function-around-a-nested-one",
    ],
)
def test_a_member_of_a_reachable_class_stays_exported(
    nested: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in nested if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert any(nested[qn] for qn in matches), symbol


def test_an_unused_member_of_a_private_nested_struct_is_dead(tmp_path: Path) -> None:
    source = """\
static int unused_callee() { return 1; }

class Outer {
private:
    struct Hidden { static int helper() { return unused_callee(); } };
};
"""
    graph = _indexed(tmp_path, {"hidden.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = collect_dead_code(_Graph(graph), graph.project, config)

    assert {row["name"] for row in dead} >= {"helper", "unused_callee"}


# Greptile on PR #2952: an access label inside a preprocessor block governs
# what follows it, and a member is private only when private in every build.
PREPROC_CPP = """\
class SameBlockNested {
#ifdef FEATURE
public:
    struct Inner { void same_block_nested() {} };
#endif
};
class SameBlockMember {
#ifdef FEATURE
public:
    void same_block_member() {}
#endif
};
class EarlierBlock {
#ifdef FEATURE
public:
#endif
    void after_earlier_block() {}
};
class NestedBlocks {
#ifdef A
#ifdef B
public:
    void nested_blocks() {}
#endif
#endif
};
class Branches {
#if A
public:
#else
private:
#endif
    void after_branches() {}
#ifdef C
private:
    void inside_private_branch() {}
#elif D
    void elif_inherits() {}
#else
public:
    void else_public() {}
#endif
};
struct Debug {
#ifdef DEBUG
private:
    void debug_only() {}
#endif
    void after_maybe_private() {}
};
class BothPrivate {
#ifdef A
private:
#else
private:
#endif
    void private_both_ways() {}
};
void ::global_qualified() {}
static void global_static();
void ::global_static() {}
"""


@pytest.fixture
def preproc(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"pp.cpp": PREPROC_CPP})


@pytest.mark.parametrize(
    "symbol",
    [
        "Inner.same_block_nested",
        "same_block_member",
        "after_earlier_block",
        "nested_blocks",
        "after_branches",
        "elif_inherits",
        "else_public",
        "after_maybe_private",
        "global_qualified",
    ],
    ids=[
        "label-and-nested-class-in-one-ifdef",
        "label-and-member-in-one-ifdef",
        "label-in-an-earlier-ifdef",
        "label-in-nested-ifdefs",
        "public-in-one-branch-private-in-the-other",
        "elif-branch-inherits-the-access-before-it",
        "else-branch-with-its-own-label",
        "after-an-ifdef-that-may-make-it-private",
        "function-qualified-by-the-global-namespace",
    ],
)
def test_public_in_some_build_is_exported(
    preproc: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in preproc if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert any(preproc[qn] for qn in matches), symbol


@pytest.mark.parametrize(
    "symbol",
    ["debug_only", "inside_private_branch", "private_both_ways", "global_static"],
    ids=[
        "private-label-inside-an-ifdef-of-a-struct",
        "inside-a-private-branch",
        "private-in-every-branch",
        "static-declared-before-a-global-qualified-definition",
    ],
)
def test_private_in_every_build_stays_private(
    preproc: dict[str, bool], symbol: str
) -> None:
    matches = [qn for qn in preproc if qn.endswith(f".{symbol}")]
    assert matches, symbol
    assert not any(preproc[qn] for qn in matches), symbol


def test_a_public_method_behind_an_ifdef_label_is_not_dead(tmp_path: Path) -> None:
    source = """\
static int helper() { return 1; }

class Outer {
#ifdef FEATURE
public:
    struct Inner { int api() { return helper(); } };
#endif
};
"""
    graph = _indexed(tmp_path, {"guarded.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    assert collect_dead_code(_Graph(graph), graph.project, config) == []


# A library template specialized for this code (`std::hash`, `fmt::formatter`)
# is called by that library, so its members are API, though its qualifier is
# no namespace of the file. A plain out-of-line nested class is not.
SPECIALIZED_CPP = """\
struct Foo { int v; };
template <class T> struct Box { T v; };

template <> struct std::hash<Foo> {
    unsigned long full_spec(const Foo& f) const { return f.v; }
};

template <class T> struct std::hash<Box<T>> {
    unsigned long partial_spec(const Box<T>& b) const { return 0; }
};

template <> struct fmt::formatter<Foo> {
    int format_foo(const Foo& f) const { return f.v; }
private:
    int private_in_spec() const { return 0; }
};

template <> struct nlohmann::adl_serializer<Foo> {
    static void to_json_foo(int& j, const Foo& f) { j = f.v; }
};

template <> struct lib::detail::trait<Foo> {
    static int deep_trait() { return 1; }
};

#ifdef WITH_FMT
template <> struct fmt::formatter<Box<int>> {
    int guarded_spec() const { return 1; }
};
#endif

class Outer {
    struct PrivateNested;
};
struct Outer::PrivateNested { void private_nested() {} };

struct ext::Thing { void ext_thing() {} };
"""
STD_INSIDE_CPP = """\
struct Bar { int v; };

namespace std {
template <> struct hash<Bar> {
    unsigned long inside_std(const Bar& b) const { return b.v; }
};
}
"""


@pytest.fixture
def specialized(tmp_path: Path) -> dict[str, bool]:
    return _run(tmp_path, {"spec.cpp": SPECIALIZED_CPP, "inside.cpp": STD_INSIDE_CPP})


@pytest.mark.parametrize(
    "symbol",
    [
        "full_spec",
        "partial_spec",
        "format_foo",
        "to_json_foo",
        "deep_trait",
        "guarded_spec",
        "inside_std",
    ],
    ids=[
        "full-specialization-qualified-by-std",
        "partial-specialization-qualified-by-std",
        "fmt-formatter-specialization",
        "static-member-of-an-adl-serializer-specialization",
        "specialization-qualified-by-two-namespaces",
        "specialization-inside-an-ifdef",
        "specialization-written-inside-namespace-std",
    ],
)
def test_a_library_specialization_member_is_exported(
    specialized: dict[str, bool], symbol: str
) -> None:
    assert _one(specialized, f".{symbol}") is True


@pytest.mark.parametrize(
    "symbol",
    ["private_in_spec", "private_nested", "ext_thing"],
    ids=[
        "private-member-of-a-specialization",
        "private-nested-class-defined-out-of-line",
        "non-template-class-defined-out-of-line",
    ],
)
def test_out_of_line_classes_that_are_no_specialization_stay_private(
    specialized: dict[str, bool], symbol: str
) -> None:
    assert _one(specialized, f".{symbol}") is False


def test_a_formatter_specialization_and_its_helper_are_not_dead(
    tmp_path: Path,
) -> None:
    source = """\
struct Foo { int v; };

static int render(int v) { return v * 31; }

template <> struct fmt::formatter<Foo> {
    int format(const Foo& f) const { return render(f.v); }
};
"""
    graph = _indexed(tmp_path, {"formatter.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    assert collect_dead_code(_Graph(graph), graph.project, config) == []


# Greptile on PR #2952: blocks that test the same flag are in or out
# together, and a specialization of a class's member template takes that
# class's access.
CORRELATED_CPP = """\
class SameFlag {
#ifdef FEATURE
public:
#endif
#ifdef FEATURE
private:
#endif
    void same_flag() {}
};
class SameExpression {
#if LEVEL > 1
public:
#endif
#if LEVEL > 1
private:
#endif
    void same_expression() {}
};
class OnlyWithoutLabel {
#ifdef FEATURE
public:
#endif
#ifndef FEATURE
    void only_without_label() {}
#endif
};
class DefinedSpellings {
#ifdef FEATURE
public:
#endif
#if !defined(FEATURE)
    void defined_spellings() {}
#endif
};
class OnlyWithLabel {
#ifdef FEATURE
public:
#endif
#if defined FEATURE
    void only_with_label() {}
#endif
};
class OtherFlags {
#ifdef A
public:
#endif
#ifdef B
private:
#endif
    void other_flags() {}
};
"""
MEMBER_TEMPLATE_CPP = """\
class Outer { template <class T> struct Hidden; };
template <> struct Outer::Hidden<int> { void member_template_spec() {} };
"""


@pytest.fixture
def correlated(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path, {"flags.cpp": CORRELATED_CPP, "member.cpp": MEMBER_TEMPLATE_CPP}
    )


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("same_flag", False),
        ("same_expression", False),
        ("only_without_label", False),
        ("defined_spellings", False),
        ("member_template_spec", False),
        ("only_with_label", True),
        ("other_flags", True),
    ],
    ids=[
        "public-then-private-under-one-flag",
        "public-then-private-under-one-expression",
        "compiled-only-without-the-public-label",
        "ifdef-and-not-defined-are-one-flag",
        "specialized-private-member-template-of-a-class",
        "compiled-only-with-the-public-label",
        "different-flags-stay-independent",
    ],
)
def test_blocks_on_one_flag_agree(
    correlated: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(correlated, f".{symbol}") is is_exported


def test_a_method_private_in_every_build_is_dead(tmp_path: Path) -> None:
    source = """\
static int callee() { return 1; }

class Box {
#ifdef FEATURE
public:
#endif
#ifdef FEATURE
private:
#endif
    int unused() { return callee(); }
};
"""
    graph = _indexed(tmp_path, {"box.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = collect_dead_code(_Graph(graph), graph.project, config)

    assert {row["name"] for row in dead} >= {"unused", "callee"}


# Greptile on PR #2952: a `#define` or `#undef` inside the class changes the
# macro between two blocks that test it, so those blocks need not agree; and
# a specialization's qualifier names a class of this file only where C++
# name lookup finds one, not wherever a class of that name lives.
REDEFINED_CPP = """\
class Undefined {
#ifdef FEATURE
public:
#endif
#undef FEATURE
#ifdef FEATURE
private:
#endif
    void after_undef() {}
};
class DefinedBetween {
#ifndef LATE
public:
#endif
#define LATE
#ifndef LATE
private:
#endif
    void after_define() {}
};
class UndefinedInNested {
#ifdef NESTED
public:
#endif
    struct Inner {
#undef NESTED
    };
#ifdef NESTED
private:
#endif
    void after_nested_undef() {}
};
class ExpressionUndefined {
#if LEVEL > 1
public:
#endif
#undef LEVEL
#if LEVEL > 1
private:
#endif
    void after_expression_undef() {}
};
class UnrelatedDefine {
#ifdef FEATURE
public:
#endif
#define HELPER 1
#ifdef FEATURE
private:
#endif
    void after_unrelated_define() {}
};
"""
SCOPED_QUALIFIER_CPP = """\
namespace app { struct fmt {}; }
struct Foo { int v; };
struct Bar { int v; };

template <> struct fmt::formatter<Foo> {
    int format_beside_app_fmt(const Foo& f) const { return f.v; }
};

template <> struct ::fmt::formatter<Bar> {
    int format_from_global(const Bar& b) const { return b.v; }
};

namespace app {
class own { template <class T> struct formatter; };
template <> struct own::formatter<int> { void own_member_template() {} };
}

namespace lib { class V {}; }
namespace V { void beside_lib_v(); }
void V::beside_lib_v() {}
"""


@pytest.fixture
def redefined(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {"redefined.cpp": REDEFINED_CPP, "scoped.cpp": SCOPED_QUALIFIER_CPP},
    )


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("after_undef", True),
        ("after_define", True),
        ("after_nested_undef", True),
        ("after_expression_undef", True),
        ("after_unrelated_define", False),
        ("format_beside_app_fmt", True),
        ("format_from_global", True),
        ("own_member_template", False),
        ("beside_lib_v", True),
    ],
    ids=[
        "undef-between-two-tests-of-one-flag",
        "define-between-two-tests-of-one-flag",
        "undef-in-a-nested-class-between-the-tests",
        "undef-between-two-tests-of-one-expression",
        "define-of-another-macro-keeps-the-flags-together",
        "qualifier-named-like-a-class-of-another-namespace",
        "qualifier-from-the-global-namespace",
        "qualifier-naming-a-class-of-the-enclosing-namespace",
        "namespace-function-named-like-a-class-of-another-namespace",
    ],
)
def test_macro_changes_and_qualifier_lookup(
    redefined: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(redefined, f".{symbol}") is is_exported


def test_a_public_method_after_an_undef_is_not_dead(tmp_path: Path) -> None:
    source = """\
static int helper() { return 1; }

class Box {
#ifdef FEATURE
public:
#endif
#undef FEATURE
#ifdef FEATURE
private:
#endif
    int api() { return helper(); }
};
"""
    graph = _indexed(tmp_path, {"box.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    assert collect_dead_code(_Graph(graph), graph.project, config) == []


def test_a_formatter_beside_a_class_of_that_name_is_not_dead(tmp_path: Path) -> None:
    source = """\
namespace app { struct fmt {}; }
struct Foo { int v; };

static int render(int v) { return v * 31; }

template <> struct fmt::formatter<Foo> {
    int format(const Foo& f) const { return render(f.v); }
};
"""
    graph = _indexed(tmp_path, {"formatter.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    assert collect_dead_code(_Graph(graph), graph.project, config) == []


# Greptile on PR #2952: one build has to expose a member in every class and
# `#if` block around it at once, and a qualifier is looked up through
# namespace aliases, using-directives and using-declarations, type aliases
# and unnamed namespaces before it is called a class.
ONE_BUILD_CPP = """\
class OuterFlag {
#ifdef FEATURE
public:
#endif
    class Inner {
#ifndef FEATURE
    public:
#endif
        void opposite_flags() {}
    };
};
class OuterSame {
#ifdef FEATURE
public:
#endif
    class Inner {
#ifdef FEATURE
    public:
#endif
        void same_flags_nested() {}
    };
};
class OuterBlock {
public:
#ifdef FEATURE
    class Inner {
#ifndef FEATURE
    public:
#endif
        void block_opposite() {}
    };
#endif
};
#ifndef WRAPPED
class Wrapped {
#ifdef WRAPPED
public:
#endif
    void wrapper_opposite() {}
};
#endif
class DefinedFirst {
#define EARLY
#ifdef EARLY
public:
#endif
#ifdef EARLY
private:
#endif
    void defined_before_tests() {}
};
"""
GUARDED_H = """\
#ifndef GUARDED_H
#define GUARDED_H
class Guarded {
#ifdef FEATURE
public:
#endif
    void guarded_public() {}
};
class GuardedPrivate {
#ifdef FEATURE
public:
#endif
#ifdef FEATURE
private:
#endif
    void guarded_private() {}
};
#endif
"""
LOOKUP_CPP = """\
namespace N { void alias_api(); }
namespace Alias = N;
void Alias::alias_api() {}

namespace outer { namespace inner { void nested_alias_api(); } }
namespace AN = outer::inner;
void AN::nested_alias_api() {}

namespace ext = lib::detail;
void ext::external_alias_api() {}

namespace Q { namespace W { void using_api(); } }
using namespace Q;
void W::using_api() {}

namespace { namespace Hidden { void anon_api(); } }
void Hidden::anon_api() {}

namespace S { static void via_alias(); }
namespace SA = S;
void SA::via_alias() {}

namespace app2 { class fmt2 { template <class T> struct formatter; }; }
using app2::fmt2;
template <> struct fmt2::formatter<int> { void using_decl_member_template() {} };

class Real { template <class T> struct Nested; };
using RealAlias = Real;
template <> struct RealAlias::Nested<int> { void type_alias_member_template() {} };

namespace { class anonfmt { template <class T> struct formatter; }; }
template <> struct anonfmt::formatter<int> { void anon_class_member_template() {} };

namespace { namespace ilib { template <class T> struct trait; } }
template <> struct ilib::trait<int> { void internal_lib_spec() {} };

namespace { namespace iv { struct Box; } }
struct iv::Box { void internal_out_of_line_class() {} };
"""
FRIENDS_CPP = """\
class Point {
    int x;
    friend bool operator==(const Point& a, const Point& b) { return a.x == b.x; }
    friend void swap_points(Point& a, Point& b) { int t = a.x; a.x = b.x; b.x = t; }
};
namespace { struct AnonFriend { friend void anon_friend(AnonFriend) {} }; }
"""


@pytest.fixture
def one_build(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {
            "one_build.cpp": ONE_BUILD_CPP,
            "guarded.h": GUARDED_H,
            "lookup.cpp": LOOKUP_CPP,
            "friends.cpp": FRIENDS_CPP,
        },
    )


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("opposite_flags", False),
        ("same_flags_nested", True),
        ("block_opposite", False),
        ("wrapper_opposite", False),
        ("defined_before_tests", False),
        ("guarded_public", True),
        ("guarded_private", False),
        ("alias_api", True),
        ("nested_alias_api", True),
        ("external_alias_api", True),
        ("using_api", True),
        ("anon_api", False),
        ("via_alias", False),
        ("using_decl_member_template", False),
        ("type_alias_member_template", False),
        ("anon_class_member_template", False),
        ("internal_lib_spec", False),
        ("internal_out_of_line_class", False),
        ("operator_equal", True),
        ("swap_points", True),
        ("anon_friend", False),
    ],
    ids=[
        "nested-class-public-only-under-the-opposite-flag",
        "nested-class-public-under-the-same-flag",
        "nested-class-compiled-only-under-the-opposite-flag",
        "class-compiled-only-under-the-opposite-flag",
        "define-before-every-test-keeps-the-flags-together",
        "include-guard-keeps-a-public-build",
        "include-guard-keeps-the-flags-together",
        "namespace-alias-qualifier",
        "alias-of-a-nested-namespace",
        "alias-of-a-namespace-of-another-file",
        "qualifier-found-through-using-namespace",
        "namespace-inside-an-unnamed-namespace",
        "static-declaration-reached-through-an-alias",
        "class-qualifier-from-a-using-declaration",
        "class-qualifier-through-a-type-alias",
        "class-qualifier-inside-an-unnamed-namespace",
        "specialization-in-an-unnamed-namespace",
        "out-of-line-class-in-an-unnamed-namespace",
        "hidden-friend-operator-before-any-label",
        "hidden-friend-function-before-any-label",
        "friend-of-a-class-in-an-unnamed-namespace",
    ],
)
def test_one_build_and_full_qualifier_lookup(
    one_build: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(one_build, f".{symbol}") is is_exported


def test_a_method_no_build_exposes_is_dead(tmp_path: Path) -> None:
    source = """\
static int callee() { return 1; }

class Outer {
#ifdef FEATURE
public:
#endif
    class Inner {
#ifndef FEATURE
    public:
#endif
        int unused() { return callee(); }
    };
};
"""
    graph = _indexed(tmp_path, {"outer.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = collect_dead_code(_Graph(graph), graph.project, config)

    assert {row["name"] for row in dead} >= {"unused", "callee"}


def test_a_function_defined_through_an_alias_is_not_dead(tmp_path: Path) -> None:
    source = """\
static int helper() { return 1; }

namespace N { int api(); }
namespace Alias = N;
int Alias::api() { return helper(); }
"""
    graph = _indexed(tmp_path, {"alias.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    # The prototype `N::api` stays a node of its own, as for any namespace
    # function defined with a qualified name; this checks the definition.
    dead = {
        str(row["qualified_name"])
        for row in collect_dead_code(_Graph(graph), graph.project, config)
    }

    assert not {qn for qn in dead if qn.endswith((".Alias.api", ".helper"))}


# Greptile on PR #2952: every function that is no class member, a friend
# defined in its class included, takes the same linkage check: its namespace,
# through an alias at any part of its qualifier, lies in no unnamed
# namespace, and no earlier declaration of it there is `static`.
ONE_LINKAGE_CPP = """\
struct X;
static void friend_after_static(X&);
struct X { friend void friend_after_static(X&) {} };

namespace FN {
struct Y;
static void friend_in_namespace(Y&);
struct Y { friend void friend_in_namespace(Y&) {} };
}

namespace FV { struct V2; static void friend_of_qualified_class(V2&); }
struct FV::V2 { friend void friend_of_qualified_class(V2&) {} };

namespace { namespace FH { struct Z; } }
struct FH::Z { friend void friend_in_internal_namespace(Z&) {} };

struct Pub;
void friend_declared_extern(Pub&);
struct Pub { friend void friend_declared_extern(Pub&) {} };

namespace N3 { static void alias_inside_namespace(); }
namespace W { namespace A = N3; }
void W::A::alias_inside_namespace() {}

namespace N4 { static void chained_alias(); }
namespace B1 = N4;
namespace B2 = B1;
void B2::chained_alias() {}

namespace N5 { void later_alias_public(); }
namespace W5 { namespace A5 = N5; }
void W5::A5::later_alias_public() {}

namespace { namespace H6 { void later_alias_internal(); } }
namespace W6 { namespace A6 = H6; }
void W6::A6::later_alias_internal() {}

namespace N7 { struct Box7 { void using_decl_later_part(); }; }
namespace W7 { using N7::Box7; }
void W7::Box7::using_decl_later_part() {}

namespace outer8 { namespace { namespace deep8 { void deep_internal(); } } }
void outer8::deep8::deep_internal() {}

namespace IN { inline namespace v1 { static void inline_static(); } }
void IN::inline_static() {}

namespace { template <class T> struct Tmpl; }
template <> struct Tmpl<int> { void internal_template_spec() {} };

namespace { static void twin(int); }
void twin(int) {}

namespace NU { namespace { void unnamed_decl_qualified(); } }
void NU::unnamed_decl_qualified() {}

namespace NU2 { namespace { void unnamed_decl_unqualified(); } }
namespace NU2 { void unnamed_decl_unqualified() {} }

static void const_param(int);
void const_param(const int n) {}

class Sealed final { protected: void final_protected() {} };

namespace { template <class T> struct FT; }
template <> struct FT<int> { friend void friend_of_internal_spec(FT&) {} };

namespace { namespace detail { int hidden_twin(); } }
namespace detail { int named_twin(); }
int detail::named_twin() { return 1; }
"""


@pytest.fixture
def linkage_check(tmp_path: Path) -> dict[tuple[str, int], bool]:
    # By name and line: a definition and the declaration before it can share
    # a qualified name, which `_one` cannot tell apart.
    graph = _indexed(tmp_path, {"one_linkage.cpp": ONE_LINKAGE_CPP})
    return {
        (str(props[cs.KEY_NAME]), int(props[cs.KEY_START_LINE])): bool(
            props[cs.KEY_IS_EXPORTED]
        )
        for props in graph.nodes.values()
        if cs.KEY_IS_EXPORTED in props and cs.KEY_START_LINE in props
    }


@pytest.mark.parametrize(
    ("symbol", "line", "is_exported"),
    [
        ("friend_after_static", 3, False),
        ("friend_in_namespace", 8, False),
        ("friend_of_qualified_class", 12, False),
        ("friend_in_internal_namespace", 15, False),
        ("friend_declared_extern", 19, True),
        ("alias_inside_namespace", 23, False),
        ("chained_alias", 28, False),
        ("later_alias_public", 32, True),
        ("later_alias_internal", 36, False),
        ("using_decl_later_part", 40, False),
        ("deep_internal", 43, False),
        ("inline_static", 46, False),
        ("internal_template_spec", 49, False),
        ("twin", 52, True),
        ("unnamed_decl_qualified", 55, False),
        ("unnamed_decl_unqualified", 58, True),
        ("const_param", 61, False),
        ("final_protected", 63, False),
        ("friend_of_internal_spec", 66, False),
        ("named_twin", 70, True),
    ],
    ids=[
        "friend-after-a-static-declaration",
        "friend-after-a-static-declaration-in-its-namespace",
        "friend-of-a-class-defined-through-its-namespace",
        "friend-of-a-class-in-an-unnamed-namespace",
        "friend-after-an-extern-declaration",
        "alias-in-a-later-part-of-the-qualifier",
        "alias-of-an-alias",
        "alias-in-a-later-part-naming-a-public-namespace",
        "alias-in-a-later-part-naming-an-unnamed-namespace-one",
        "using-declaration-in-a-later-part",
        "unnamed-namespace-deep-inside-a-named-one",
        "static-declaration-in-an-inline-namespace",
        "specialization-of-a-template-in-an-unnamed-namespace",
        "static-declaration-of-another-function-in-an-unnamed-namespace",
        "qualified-definition-of-a-function-of-an-unnamed-namespace",
        "unqualified-definition-beside-one-of-an-unnamed-namespace",
        "static-declaration-spelling-a-parameter-without-const",
        "protected-member-of-a-final-class",
        "friend-of-a-specialization-of-a-template-in-an-unnamed-namespace",
        "namespace-named-like-one-inside-an-unnamed-namespace",
    ],
)
def test_one_linkage_check_for_every_function(
    linkage_check: dict[tuple[str, int], bool],
    symbol: str,
    line: int,
    is_exported: bool,
) -> None:
    assert linkage_check[(symbol, line)] is is_exported


def test_a_friend_after_a_static_declaration_is_dead(tmp_path: Path) -> None:
    source = """\
static int callee() { return 1; }

struct X;
static int unused(X&);
struct X { friend int unused(X&) { return callee(); } };
"""
    graph = _indexed(tmp_path, {"friend.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = collect_dead_code(_Graph(graph), graph.project, config)

    assert {row["name"] for row in dead} >= {"unused", "callee"}


def test_a_private_function_defined_through_a_nested_alias_is_dead(
    tmp_path: Path,
) -> None:
    source = """\
namespace N { static int helper(); }
namespace W { namespace A = N; }
int W::A::helper() { return 1; }
"""
    graph = _indexed(tmp_path, {"nested_alias.cpp": source})
    config = default_dead_code_config(include_tests=True, include_classes=False)

    dead = {
        str(row["qualified_name"])
        for row in collect_dead_code(_Graph(graph), graph.project, config)
    }

    assert any(qn.endswith(".W.A.helper") for qn in dead)


# Code no build compiles is no API: under `#if 0`, or after `#if 1` in its
# chain. And a Scala 3 extension method is a member of where its
# `extension` stands.
LITERAL_CPP = """\
#if 0
void under_if_zero() {}
#endif
#if 1
void under_if_one() {}
#else
void after_if_one() {}
#endif
class Literal {
#if 0
public:
#endif
    void label_under_if_zero() {}
#if 1
public:
#endif
    void label_under_if_one() {}
};
"""
LITERAL_C = """\
#if 0
int c_under_if_zero(void) { return 0; }
#endif
"""
EXTENSIONS_SCALA = """\
object Ext {
  extension (x: Int) def twice: Int = x * 2
  extension (s: String)
    private def hiddenExt: Int = 1
    def shout: String = s
}
extension (x: Long) def topExt: Long = x
private object Closed { extension (x: Int) def closedExt: Int = x }
"""


@pytest.fixture
def literal(tmp_path: Path) -> dict[str, bool]:
    return _run(
        tmp_path,
        {
            "literal.cpp": LITERAL_CPP,
            "literal_c.c": LITERAL_C,
            "Ext.scala": EXTENSIONS_SCALA,
        },
    )


@pytest.mark.parametrize(
    ("symbol", "is_exported"),
    [
        ("under_if_zero", False),
        ("under_if_one", True),
        ("after_if_one", False),
        ("label_under_if_zero", False),
        ("label_under_if_one", True),
        ("c_under_if_zero", False),
        ("twice", True),
        ("shout", True),
        ("topExt", True),
        ("hiddenExt", False),
        ("closedExt", False),
    ],
    ids=[
        "function-under-if-zero",
        "function-under-if-one",
        "function-after-if-one",
        "public-label-under-if-zero",
        "public-label-under-if-one",
        "c-function-under-if-zero",
        "scala-extension-method-in-an-object",
        "scala-extension-method-of-a-block",
        "scala-top-level-extension-method",
        "scala-private-extension-method",
        "scala-extension-method-in-a-private-object",
    ],
)
def test_dead_branches_and_extension_methods(
    literal: dict[str, bool], symbol: str, is_exported: bool
) -> None:
    assert _one(literal, f".{symbol}") is is_exported
