# A struct defined inside a C++ function body (a functor, a visitor, a
# comparator) had no place in the graph. An anonymous one (`struct { void
# operator()(int) } enter_state;`, fmt's parse_format_specs) got no node at
# all, while the call pass still credited its body's calls to a phantom
# `module.operator_call` caller, so those CALLS rows failed to write. A named
# one landed at module level, where two functions each defining a `Checker`
# collide. And every call inside a local struct's methods was ALSO recorded as
# a direct call of the enclosing function (issue #2555).
#
# A local type is now scoped under the callable it is written in, the way a
# PHP anonymous class is (`<enclosing callable qn>.anonymous_<row>_<col>`,
# row/col 0-based) and the way a Python/Java local class already is
# (`<enclosing callable qn>.<Name>`); its members are Methods of it, and a call
# on a functor object binds to that type's operator().
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.tests.conftest import (
    get_node_names,
    get_relationships,
    run_updater,
)

_PROJECT = "cpplocal"

# The issue's repro, verbatim. The anonymous struct opens at row 4, col 2.
_PARSE = """\
void report_error(const char* msg) { (void)msg; }
int validate(int x) { return x > 0 ? x : 0; }

int parse(int n) {
  struct {                                  // anonymous local struct (fmt's parse_format_specs)
    int state = 0;
    void operator()(int s) {
      if (state >= s) report_error("bad state");
      state = s;
    }
  } enter_state;

  struct Checker {                          // named local struct
    int check(int v) { return validate(v); }
  } checker;

  enter_state(n);
  return checker.check(n);
}
"""

_M = f"{_PROJECT}.parse"
_PARSE_FN = f"{_M}.parse"
_ANON = f"{_PARSE_FN}.anonymous_4_2"
_CHECKER = f"{_PARSE_FN}.Checker"

# A local comparator handed to an algorithm, called as a temporary, and
# through a named local of its type.
_SORT = """\
#include <algorithm>
#include <vector>

int weight(int x) { return x; }

void sort_desc(std::vector<int>& v) {
  struct Cmp {
    bool operator()(int a, int b) const { return weight(a) > weight(b); }
  };
  std::sort(v.begin(), v.end(), Cmp{});
  Cmp{}(1, 2);
  Cmp by_weight;
  std::stable_sort(v.begin(), v.end(), by_weight);
}
"""

_SORT_M = f"{_PROJECT}.sort"
_CMP = f"{_SORT_M}.sort_desc.Cmp"

# Two functions in one file, each with its own local `Checker`.
_TWIN = """\
int first(int x) { return x; }
int second(int x) { return x; }

int f(int n) {
  struct Checker { int check(int v) { return first(v); } } c;
  return c.check(n);
}

int g(int n) {
  struct Checker { int check(int v) { return second(v); } } c;
  return c.check(n);
}
"""

_TWIN_M = f"{_PROJECT}.twin"

# Local structs inside an inline method and an out-of-class method.
_WIDGET = """\
namespace ui {
class Widget {
public:
  int inline_run(int n) {
    struct Step { int go(int v) { return v + 1; } } step;
    return step.go(n);
  }
  int run(int n);
};

int Widget::run(int n) {
  struct Visitor { int visit(int v) { return v * 2; } } visitor;
  return visitor.visit(n);
}
}
"""

_WIDGET_CLASS = f"{_PROJECT}.widget.ui.Widget"


def _index(temp_repo: Path, mock_ingestor: MagicMock, **files: str) -> None:
    project = temp_repo / _PROJECT
    project.mkdir(exist_ok=True)
    for name, body in files.items():
        (project / f"{name}.cpp").write_text(body, encoding="utf-8")
    run_updater(project, mock_ingestor)


def _edges(mock_ingestor: MagicMock, rel: RelationshipType) -> set[tuple[str, str]]:
    return {
        (str(c.args[0][2]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, rel)
    }


def _labelled_edges(
    mock_ingestor: MagicMock, rel: RelationshipType
) -> set[tuple[str, str, str, str]]:
    return {
        (str(c.args[0][0]), str(c.args[0][2]), str(c.args[2][0]), str(c.args[2][2]))
        for c in get_relationships(mock_ingestor, rel)
    }


def test_anonymous_local_struct_is_a_class_under_its_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    assert _ANON in get_node_names(mock_ingestor, NodeLabel.CLASS)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.FUNCTION, _PARSE_FN, NodeLabel.CLASS, _ANON) in defines


def test_anonymous_local_struct_operator_call_is_its_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    operator_call = f"{_ANON}.operator_call"
    assert operator_call in get_node_names(mock_ingestor, NodeLabel.METHOD)
    defines_method = _edges(mock_ingestor, RelationshipType.DEFINES_METHOD)
    assert (_ANON, operator_call) in defines_method
    functions = get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    assert not {qn for qn in functions if qn.endswith(".operator_call")}, functions


def test_anonymous_struct_method_owns_its_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's failed CALLS row: `parse.operator_call -> report_error`
    # had no caller node. run_updater's graph audit rejects any such edge.
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    calls = _labelled_edges(mock_ingestor, RelationshipType.CALLS)
    assert (
        NodeLabel.METHOD,
        f"{_ANON}.operator_call",
        NodeLabel.FUNCTION,
        f"{_M}.report_error",
    ) in calls


def test_named_local_struct_is_scoped_under_its_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert _CHECKER in classes
    assert f"{_M}.Checker" not in classes
    assert f"{_CHECKER}.check" in get_node_names(mock_ingestor, NodeLabel.METHOD)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.FUNCTION, _PARSE_FN, NodeLabel.CLASS, _CHECKER) in defines
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{_CHECKER}.check", f"{_M}.validate") in calls


def test_local_struct_method_calls_do_not_leak_to_enclosing_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    parse_calls = {
        dst
        for src, dst in _edges(mock_ingestor, RelationshipType.CALLS)
        if src == _PARSE_FN
    }
    assert f"{_M}.report_error" not in parse_calls
    assert f"{_M}.validate" not in parse_calls


def test_functor_typed_local_call_binds_to_its_operator_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's expected row for parse: `operator()@17`, `check@18`.
    _index(temp_repo, mock_ingestor, parse=_PARSE)

    parse_calls = {
        dst
        for src, dst in _edges(mock_ingestor, RelationshipType.CALLS)
        if src == _PARSE_FN
    }
    assert parse_calls == {f"{_ANON}.operator_call", f"{_CHECKER}.check"}


def test_local_comparator_passed_to_an_algorithm_binds_its_operator_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A comparator handed to an algorithm (`Cmp{}`, and the `by_weight`
    # local) references the operator() the algorithm runs, which keeps it
    # reachable; the pass itself is no invocation by `sort_desc`.
    _index(temp_repo, mock_ingestor, sort=_SORT)

    assert _CMP in get_node_names(mock_ingestor, NodeLabel.CLASS)
    references = _edges(mock_ingestor, RelationshipType.REFERENCES)
    assert (f"{_SORT_M}.sort_desc", f"{_CMP}.operator_call") in references
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{_CMP}.operator_call", f"{_SORT_M}.weight") in calls
    assert (f"{_SORT_M}.sort_desc", f"{_SORT_M}.weight") not in calls


def test_temporary_and_named_local_functor_calls_bind_operator_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Cmp{}(1, 2)` alone in a function, so its edge is not the comparator
    # argument's.
    source = """\
int weight(int x) { return x; }

bool tie(int a) {
  struct Cmp {
    bool operator()(int x, int y) const { return weight(x) == y; }
  };
  return Cmp{}(a, a);
}

bool named(int a) {
  struct Eq { bool operator()(int x, int y) const { return x == y; } } eq;
  return eq(a, a);
}
"""
    _index(temp_repo, mock_ingestor, tie=source)

    module = f"{_PROJECT}.tie"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.tie", f"{module}.tie.Cmp.operator_call") in calls
    assert (f"{module}.named", f"{module}.named.Eq.operator_call") in calls


def test_namespace_level_functor_binds_operator_call_too(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The binding is by the object's type, so a functor declared at
    # namespace scope links the same way; its node stays where it was.
    source = """\
int weight(int x) { return x; }
struct Fn { void operator()(int s) { weight(s); } };

void apply(int n) {
  Fn f;
  f(n);
}
"""
    _index(temp_repo, mock_ingestor, ns=source)

    module = f"{_PROJECT}.ns"
    assert get_node_names(mock_ingestor, NodeLabel.CLASS) == {f"{module}.Fn"}
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.apply", f"{module}.Fn.operator_call") in calls
    assert (f"{module}.apply", f"{module}.weight") not in calls


def test_same_named_local_structs_in_two_functions_stay_apart(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, twin=_TWIN)

    f_checker = f"{_TWIN_M}.f.Checker"
    g_checker = f"{_TWIN_M}.g.Checker"
    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert {f_checker, g_checker} <= classes
    assert not {qn for qn in classes if "@" in qn}, classes
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{_TWIN_M}.f", f"{f_checker}.check") in calls
    assert (f"{_TWIN_M}.g", f"{g_checker}.check") in calls
    assert (f"{_TWIN_M}.f", f"{g_checker}.check") not in calls
    assert (f"{_TWIN_M}.g", f"{f_checker}.check") not in calls
    assert (f"{f_checker}.check", f"{_TWIN_M}.first") in calls
    assert (f"{g_checker}.check", f"{_TWIN_M}.second") in calls


def test_local_struct_in_inline_method_is_scoped_under_the_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, widget=_WIDGET)

    method = f"{_WIDGET_CLASS}.inline_run"
    step = f"{method}.Step"
    assert step in get_node_names(mock_ingestor, NodeLabel.CLASS)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.METHOD, method, NodeLabel.CLASS, step) in defines
    assert (method, f"{step}.go") in _edges(mock_ingestor, RelationshipType.CALLS)


def test_local_struct_in_out_of_class_method_is_scoped_under_the_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, widget=_WIDGET)

    method = f"{_WIDGET_CLASS}.run"
    visitor = f"{method}.Visitor"
    assert visitor in get_node_names(mock_ingestor, NodeLabel.CLASS)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.METHOD, method, NodeLabel.CLASS, visitor) in defines
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (method, f"{visitor}.visit") in calls


def test_nested_local_types_hang_off_their_local_parent(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    source = """\
int g1(int x) { return x; }

int nested(int n) {
  struct A {
    struct B { int m(int v) { return g1(v); } } b;
    int k(int v) { return b.m(v); }
  } a;
  struct { struct { int z(int v) { return g1(v); } } in; } out;
  return a.k(n) + out.in.z(n);
}
"""
    _index(temp_repo, mock_ingestor, nest=source)

    fn = f"{_PROJECT}.nest.nested"
    outer_anon = f"{fn}.anonymous_7_2"
    inner_anon = f"{outer_anon}.anonymous_7_11"
    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert {f"{fn}.A", f"{fn}.A.B", outer_anon, inner_anon} <= classes
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.CLASS, f"{fn}.A", NodeLabel.CLASS, f"{fn}.A.B") in defines
    assert (NodeLabel.CLASS, outer_anon, NodeLabel.CLASS, inner_anon) in defines
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{fn}.A.k", f"{fn}.A.B.m") in calls
    assert (f"{inner_anon}.z", f"{_PROJECT}.nest.g1") in calls
    assert (fn, f"{_PROJECT}.nest.g1") not in calls


def test_overloads_keep_their_own_local_structs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The second `over` registers as `over@4`; its local `L` is written in
    # it, so it lives under that qn and its caller binds to it, not to the
    # first overload's `L`.
    source = """\
int g1(int x) { return x; }
int g2(int x) { return x; }
int over(int n) { struct L { int a(int v) { return g1(v); } } l; return l.a(n); }
int over(double d) { struct L { int a(int v) { return g2(v); } } l; return l.a(1); }
"""
    _index(temp_repo, mock_ingestor, over=source)

    module = f"{_PROJECT}.over"
    first, second = f"{module}.over", f"{module}.over@4"
    assert {f"{first}.L", f"{second}.L"} <= get_node_names(
        mock_ingestor, NodeLabel.CLASS
    )
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.FUNCTION, second, NodeLabel.CLASS, f"{second}.L") in defines
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (first, f"{first}.L.a") in calls
    assert (second, f"{second}.L.a") in calls
    assert (second, f"{first}.L.a") not in calls


# --- Negative: neighbouring shapes stay exactly as they were. These hold on
# main too; they guard what the fix must not move. ---

_NESTED = """\
int helper(int x) { return x; }

namespace ns {
struct Outer {
  struct Inner {
    int go(int v) { return helper(v); }
  };
  int run(int v) { return helper(v); }
};
}
"""


def test_namespace_level_and_class_nested_structs_are_unchanged(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, nested=_NESTED)

    module = f"{_PROJECT}.nested"
    outer = f"{module}.ns.Outer"
    inner = f"{outer}.Inner"
    assert get_node_names(mock_ingestor, NodeLabel.CLASS) == {outer, inner}
    assert get_node_names(mock_ingestor, NodeLabel.METHOD) == {
        f"{outer}.run",
        f"{inner}.go",
    }
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    class_defines = {edge for edge in defines if edge[2] == NodeLabel.CLASS}
    assert class_defines == {
        (NodeLabel.MODULE, module, NodeLabel.CLASS, outer),
        (NodeLabel.CLASS, outer, NodeLabel.CLASS, inner),
    }
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert calls == {
        (f"{inner}.go", f"{module}.helper"),
        (f"{outer}.run", f"{module}.helper"),
    }


_PLAIN = """\
int helper(int x) { return x; }
int other(int x) { return x; }

int compute(int n) {
  struct { int a; int b; } pair{n, n};
  struct Point { int x; int y; };
  Point p{n, n};
  auto twice = [](int v) { return other(v); };
  return helper(pair.a + p.x) + twice(n);
}
"""


def test_local_structs_without_methods_emit_no_new_nodes(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An anonymous data-only struct stays nodeless (there is nothing in it to
    # attribute a call to), and a named one is still one Class with no
    # Method under it.
    _index(temp_repo, mock_ingestor, plain=_PLAIN)

    classes = get_node_names(mock_ingestor, NodeLabel.CLASS)
    assert not {qn for qn in classes if "anonymous_" in qn}, classes
    assert [qn.rsplit(".", 1)[-1] for qn in classes] == ["Point"]
    assert get_node_names(mock_ingestor, NodeLabel.METHOD) == set()


def test_enclosing_function_keeps_its_own_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Its direct call stays, and so does the call inside its lambda, which
    # has always been credited to the enclosing function; the lambda keeps
    # its module-anchored name.
    _index(temp_repo, mock_ingestor, plain=_PLAIN)

    module = f"{_PROJECT}.plain"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    compute_calls = {dst for src, dst in calls if src == f"{module}.compute"}
    assert compute_calls == {f"{module}.helper", f"{module}.other"}
    lambda_qn = f"{module}.lambda_7_15"
    assert lambda_qn in get_node_names(mock_ingestor, NodeLabel.FUNCTION)
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.MODULE, module, NodeLabel.FUNCTION, lambda_qn) in defines


_PLAIN_CALLS = """\
#include <algorithm>
#include <vector>

bool less_fn(int a, int b) { return a < b; }
int make(int x) { return x; }
int use(int x) { return x; }

void run(std::vector<int>& v) {
  std::sort(v.begin(), v.end(), less_fn);
  use(make(1));
}
"""


def test_function_arguments_and_plain_calls_resolve_as_before(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A function handed to an algorithm, and a call's result passed on, keep
    # their name-based edges; only an object of class type is a functor.
    _index(temp_repo, mock_ingestor, calls=_PLAIN_CALLS)

    module = f"{_PROJECT}.calls"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    run_calls = {dst for src, dst in calls if src == f"{module}.run"}
    assert run_calls == {f"{module}.less_fn", f"{module}.make", f"{module}.use"}
    assert not {dst for _src, dst in calls if dst.endswith(".operator_call")}


# A type nested in a local type is in scope inside that type's members, and
# there it hides a same-named namespace-level type, as C++ name lookup does
# (#2631 review).
_NESTED_HIDES = """\
struct B { int ping() { return 1; } };

int f() {
  struct A {
    struct B { int ping() { return 2; } } b;
    int run() { return b.ping(); }
    int fresh() { B other; return other.ping(); }
  } a;
  struct C {
    struct B { int ping() { return 3; } };
    B held;
    int use() { return held.ping(); }
  } c;
  return a.run() + a.fresh() + c.use();
}

int top() {
  B outside;
  return outside.ping();
}
"""


def test_nested_local_type_hides_a_same_named_module_type_in_its_members(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `struct B {...} b;`: the field is typed by the nested local `B`.
    _index(temp_repo, mock_ingestor, hides=_NESTED_HIDES)

    module = f"{_PROJECT}.hides"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.run", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.run", f"{module}.B.ping") not in calls


def test_field_of_a_nested_local_type_binds_to_that_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `struct B {...}; B held;`: the field's type is looked up in `C` first.
    _index(temp_repo, mock_ingestor, hides=_NESTED_HIDES)

    module = f"{_PROJECT}.hides"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.C.use", f"{module}.f.C.B.ping") in calls
    assert (f"{module}.f.C.use", f"{module}.B.ping") not in calls
    assert (f"{module}.f.C.use", f"{module}.f.A.B.ping") not in calls


def test_local_of_a_nested_local_type_binds_to_that_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, hides=_NESTED_HIDES)

    module = f"{_PROJECT}.hides"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.fresh", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.fresh", f"{module}.B.ping") not in calls


def test_module_type_outside_the_local_scope_still_binds_to_itself(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the nested local `B` is visible only inside `A`; a function
    # elsewhere still reaches the namespace-level `B`.
    _index(temp_repo, mock_ingestor, hides=_NESTED_HIDES)

    module = f"{_PROJECT}.hides"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.top", f"{module}.B.ping") in calls
    assert (f"{module}.top", f"{module}.f.A.B.ping") not in calls
    assert (f"{module}.top", f"{module}.f.C.B.ping") not in calls
    assert (f"{module}.f", f"{module}.f.A.run") in calls
    assert (f"{module}.f", f"{module}.f.C.use") in calls


# A functor handed to a call is passed as a value: whether the callee runs it
# (`std::sort`) or only keeps it (`push_back`) is not visible at the call
# site, so the pass is a reference, never an invocation (#2631 review).
_STORED = """\
#include <functional>
#include <vector>

int weight(int x) { return x; }

void keep(std::vector<std::function<void(int)>>& values) {
  struct Fn { void operator()(int s) const { weight(s); } } functor;
  values.push_back(functor);
  values.push_back(Fn{});
}

void invoke(int n) {
  struct Go { void operator()(int s) const { weight(s); } } go;
  go(n);
}
"""


def test_stored_functor_is_referenced_not_called(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, stored=_STORED)

    module = f"{_PROJECT}.stored"
    operator_call = f"{module}.keep.Fn.operator_call"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.keep", operator_call) not in calls
    references = _edges(mock_ingestor, RelationshipType.REFERENCES)
    assert (f"{module}.keep", operator_call) in references


def test_invoked_functor_still_calls_its_operator(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: calling the functor object is an invocation, and stays CALLS.
    _index(temp_repo, mock_ingestor, stored=_STORED)

    module = f"{_PROJECT}.stored"
    operator_call = f"{module}.invoke.Go.operator_call"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.invoke", operator_call) in calls
    references = _edges(mock_ingestor, RelationshipType.REFERENCES)
    assert (f"{module}.invoke", operator_call) not in references


# Two out-of-class overloads each write a local `L` (#2631 re-review): each
# overload's calls bind its own `L`, never the first overload's.
_OUT_OF_CLASS_OVERLOADS = """\
int g1(int x) { return x; }
int g2(int x) { return x; }

namespace ui {
class Widget {
public:
  int run(int n);
  int run(double d);
};

int Widget::run(int n) {
  struct L { int a(int v) { return g1(v); } } l;
  return l.a(n);
}

int Widget::run(double d) {
  struct L { int a(int v) { return g2(v); } } l;
  return l.a(1);
}
}
"""


def test_out_of_class_overloads_keep_their_own_local_structs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Out-of-class overloads share one `Widget.run` Method node; the second
    # one's local `L` sits under `run@16`, the line it starts on, and its
    # `l.a(1)` binds that `L`, not the first overload's.
    _index(temp_repo, mock_ingestor, ooc=_OUT_OF_CLASS_OVERLOADS)

    run = f"{_PROJECT}.ooc.ui.Widget.run"
    first_l, second_l = f"{run}.L", f"{run}@16.L"
    assert {first_l, second_l} <= get_node_names(mock_ingestor, NodeLabel.CLASS)
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{first_l}.a", f"{_PROJECT}.ooc.g1") in calls
    assert (f"{second_l}.a", f"{_PROJECT}.ooc.g2") in calls
    assert (run, f"{first_l}.a") in calls
    assert (run, f"{second_l}.a") in calls


def test_out_of_class_overload_local_struct_is_defined_by_its_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: both local `L`s still hang off the one `Widget.run` node, and
    # a single out-of-class definition keeps its unmarked name (asserted by
    # test_local_struct_in_out_of_class_method_is_scoped_under_the_method).
    _index(temp_repo, mock_ingestor, ooc=_OUT_OF_CLASS_OVERLOADS)

    run = f"{_PROJECT}.ooc.ui.Widget.run"
    defines = _labelled_edges(mock_ingestor, RelationshipType.DEFINES)
    assert (NodeLabel.METHOD, run, NodeLabel.CLASS, f"{run}.L") in defines
    assert (NodeLabel.METHOD, run, NodeLabel.CLASS, f"{run}@16.L") in defines


# A receiver whose type is spelled qualified names that type, not the local
# one the bare name would find (#2631 re-review).
_QUALIFIED_RECEIVERS = """\
struct B { int ping() { return 1; } };
namespace ns { struct C { int ping() { return 3; } }; }

int f() {
  struct A {
    struct B { int ping() { return 2; } };
    struct C { int ping() { return 4; } };
    ::B field;
    int global_b() { ::B g; return g.ping(); }
    int ns_c(ns::C n) { return n.ping(); }
    int member() { return field.ping(); }
    int local_b() { B l; return l.ping(); }
  } a;
  return a.global_b() + a.ns_c(ns::C{}) + a.member() + a.local_b();
}
"""


def test_qualified_receiver_types_bind_the_named_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A local, a parameter and a field each spelled `::B` / `ns::C` name
    # the class the qualifier names, not `A`'s nested one.
    _index(temp_repo, mock_ingestor, qual=_QUALIFIED_RECEIVERS)

    module = f"{_PROJECT}.qual"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    for caller, wrong in (
        ("global_b", "B"),
        ("ns_c", "C"),
        ("member", "B"),
    ):
        assert (f"{module}.f.A.{caller}", f"{module}.f.A.{wrong}.ping") not in calls
    assert (f"{module}.f.A.global_b", f"{module}.B.ping") in calls
    assert (f"{module}.f.A.ns_c", f"{module}.ns.C.ping") in calls
    assert (f"{module}.f.A.member", f"{module}.B.ping") in calls


def test_unqualified_receiver_type_still_binds_the_local_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the bare `B` inside `A` is the nested local one.
    _index(temp_repo, mock_ingestor, qual=_QUALIFIED_RECEIVERS)

    module = f"{_PROJECT}.qual"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.local_b", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.local_b", f"{module}.B.ping") not in calls


# The overloads may sit in two blocks of one reopened namespace (#2631
# re-review): the second is still the second.
_REOPENED_OVERLOADS = """\
int g1(int x) { return x; }
int g2(int x) { return x; }

namespace ui {
class Widget {
public:
  int run(int n);
  int run(double d);
};
}

namespace ui {
int Widget::run(int n) {
  struct L { int a(int v) { return g1(v); } } l;
  return l.a(n);
}
}

namespace ui {
int Widget::run(double d) {
  struct L { int a(int v) { return g2(v); } } l;
  return l.a(1);
}
}
"""


def test_overloads_in_reopened_namespace_blocks_keep_their_own_local_structs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, reopen=_REOPENED_OVERLOADS)

    run = f"{_PROJECT}.reopen.ui.Widget.run"
    second_l = f"{run}@20.L"
    assert second_l in get_node_names(mock_ingestor, NodeLabel.CLASS)
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{second_l}.a", f"{_PROJECT}.reopen.g2") in calls
    assert (run, f"{second_l}.a") in calls


def test_first_overload_in_a_reopened_namespace_keeps_its_unmarked_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: the first definition's local `L` keeps `Widget.run.L`.
    _index(temp_repo, mock_ingestor, reopen=_REOPENED_OVERLOADS)

    run = f"{_PROJECT}.reopen.ui.Widget.run"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{run}.L.a", f"{_PROJECT}.reopen.g1") in calls
    assert (run, f"{run}.L.a") in calls


# A local or parameter of a member function hides a field of the same name
# (#2631 re-review): its unqualified type is what binds.
_SHADOWED_FIELD = """\
struct B { int ping() { return 1; } };

int f() {
  struct A {
    struct B { int ping() { return 2; } };
    ::B b;
    int local() { B b; return b.ping(); }
    int param(B b) { return b.ping(); }
    int field() { return b.ping(); }
  } a;
  return a.local() + a.param(A::B{}) + a.field();
}
"""


def test_local_shadowing_a_qualified_field_binds_the_local_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, shadow=_SHADOWED_FIELD)

    module = f"{_PROJECT}.shadow"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.local", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.param", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.local", f"{module}.B.ping") not in calls


def test_unshadowed_qualified_field_still_binds_the_named_class(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: where nothing hides it, the `::B` field is `B`, not `A::B`.
    _index(temp_repo, mock_ingestor, shadow=_SHADOWED_FIELD)

    module = f"{_PROJECT}.shadow"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.field", f"{module}.B.ping") in calls
    assert (f"{module}.f.A.field", f"{module}.f.A.B.ping") not in calls


# A local declared in a block hides a field of its name only inside that
# block, and only after its declaration (#2631 re-review): before it and once
# the block closes, the name is the field again.
_BLOCK_SHADOWED_FIELD = """\
struct B { int ping() { return 1; } };

int f() {
  struct A {
    struct B { int ping() { return 2; } };
    ::B b;
    int in_block() { { B b; return b.ping(); } }
    int after_block() { { B b; (void)b; } return b.ping(); }
    int before_decl() { int r = b.ping(); B b; (void)b; return r; }
  } a;
  struct C {
    struct B { int ping() { return 3; } };
    B b;
    int after_block() { { ::B b; (void)b; } return b.ping(); }
    int in_block() { { ::B b; return b.ping(); } }
  } c;
  return a.in_block() + a.after_block() + a.before_decl() + c.after_block()
      + c.in_block();
}
"""


def test_block_local_hides_a_field_only_inside_its_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(temp_repo, mock_ingestor, block=_BLOCK_SHADOWED_FIELD)

    module = f"{_PROJECT}.block"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    for caller in ("after_block", "before_decl"):
        assert (f"{module}.f.A.{caller}", f"{module}.B.ping") in calls
        assert (f"{module}.f.A.{caller}", f"{module}.f.A.B.ping") not in calls
    assert (f"{module}.f.C.after_block", f"{module}.f.C.B.ping") in calls
    assert (f"{module}.f.C.after_block", f"{module}.B.ping") not in calls


def test_block_local_still_hides_a_field_inside_its_block(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Negative: inside the block the local's own type is what binds.
    _index(temp_repo, mock_ingestor, block=_BLOCK_SHADOWED_FIELD)

    module = f"{_PROJECT}.block"
    calls = _edges(mock_ingestor, RelationshipType.CALLS)
    assert (f"{module}.f.A.in_block", f"{module}.f.A.B.ping") in calls
    assert (f"{module}.f.A.in_block", f"{module}.B.ping") not in calls
    assert (f"{module}.f.C.in_block", f"{module}.B.ping") in calls
    assert (f"{module}.f.C.in_block", f"{module}.f.C.B.ping") not in calls
