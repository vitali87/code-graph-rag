# Issue #2554: every C++ binary/unary/update expression is named as a call to
# its operator (`a + b` -> operator_plus). When the operands are built-in
# (int, double, bool, pointers) there is no overload to find, yet the name was
# still resolved: by bare name to ANY project type overloading that operator
# (heuristic, 29% of fmt's CALLS, 573 library->test edges), or, inside the
# class that defines the operator, through the class scope to its own
# overload (exact, a fake self-recursion from `x + o.x` on `int` fields). An
# operator expression binds only through an operand whose type is a known
# first-party class with that overload; anything else emits no edge.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships

MONEY_H = """struct Money {
  long cents;
  Money operator+(const Money& o) const { return Money{cents + o.cents}; }
  bool operator<(const Money& o) const { return cents < o.cents; }
  bool operator==(const Money& o) const { return cents == o.cents; }
  bool operator!=(const Money& o) const { return !(*this == o); }
  Money& operator=(const Money& o) {
    if (this == &o) return *this;
    cents = o.cents;
    return *this;
  }
};
"""

ASSERTION_H = """struct AssertionResult {
  bool ok;
  AssertionResult operator!() const { return AssertionResult{!ok}; }
  AssertionResult& operator++() { return *this; }
};
"""

UTIL_CPP = """#include "money.h"
int add(int a, int b) { return a + b; }
bool less(double x, double y) { return x < y; }
bool neg(bool v) { return !v; }
int bump(int i) { return ++i; }
Money sum(Money a, Money b) { return a + b; }
bool before(const Money& a, const Money& b) { return a < b; }
"""

VEC_HPP = """struct Vec {
    int x, y;
    Vec operator+(const Vec& o) const { return Vec{x + o.x, y + o.y}; }
    int dot(const Vec& o) const { return x * o.x + y * o.y; }
};
"""


def _calls(mock_ingestor: MagicMock) -> list[tuple[str, str, str]]:
    edges = []
    for call in get_relationships(mock_ingestor, cs.RelationshipType.CALLS.value):
        props = call.kwargs.get("properties") or (
            call.args[3] if len(call.args) > 3 else {}
        )
        edges.append(
            (
                str(call.args[0][2]),
                str(call.args[2][2]),
                str((props or {}).get(cs.KEY_RESOLUTION)),
            )
        )
    return edges


def _operator_calls_from(
    edges: list[tuple[str, str, str]], caller: str
) -> list[tuple[str, str]]:
    return [
        (callee, resolution)
        for src, callee, resolution in edges
        if src == caller and callee.rsplit(".", 1)[-1].startswith("operator_")
    ]


def _index(temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]) -> None:
    for rel_path, source in files.items():
        path = temp_repo / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    create_and_run_updater(temp_repo, mock_ingestor, skip_if_missing="cpp")


@pytest.fixture
def issue_repo_calls(
    temp_repo: Path, mock_ingestor: MagicMock
) -> tuple[str, list[tuple[str, str, str]]]:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "money.h": MONEY_H,
            "test/assertion.h": ASSERTION_H,
            "util.cpp": UTIL_CPP,
        },
    )
    return temp_repo.name, _calls(mock_ingestor)


@pytest.mark.parametrize("caller", ["add", "less", "neg", "bump"])
def test_builtin_operand_binds_no_operator_overload(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]], caller: str
) -> None:
    project, edges = issue_repo_calls
    assert _operator_calls_from(edges, f"{project}.util.{caller}") == []


def test_builtin_operator_does_not_reach_a_test_helper(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]],
) -> None:
    project, edges = issue_repo_calls
    library_to_test = [
        (src, dst)
        for src, dst, _ in edges
        if src.startswith(f"{project}.util.") and dst.startswith(f"{project}.test.")
    ]
    assert library_to_test == []


def test_builtin_member_field_arithmetic_is_no_self_call(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]],
) -> None:
    # `cents + o.cents` / `cents < o.cents` / `!ok` on built-in fields.
    project, edges = issue_repo_calls
    for method in ("operator_plus", "operator_less", "operator_equal"):
        caller = f"{project}.money.Money.{method}"
        assert _operator_calls_from(edges, caller) == [], caller
    helper = f"{project}.test.assertion.AssertionResult.operator_not"
    assert _operator_calls_from(edges, helper) == []


def test_pointer_comparison_with_this_binds_nothing(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]],
) -> None:
    # `this == &o` compares two pointers.
    project, edges = issue_repo_calls
    caller = f"{project}.money.Money.operator_assign"
    assert _operator_calls_from(edges, caller) == []


def test_class_scope_builtin_arithmetic_binds_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue comment's shape: inside Vec, `x + o.x` and `x * o.x + ...`
    # are int arithmetic, not calls of Vec::operator+.
    _index(temp_repo, mock_ingestor, {"lib.hpp": VEC_HPP})
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert _operator_calls_from(edges, f"{project}.lib.Vec.operator_plus") == []
    assert _operator_calls_from(edges, f"{project}.lib.Vec.dot") == []


def test_template_parameter_operand_binds_no_operator(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `T` is no class here, so the project's only operator== is a guess.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "keep.hpp": (
                "struct Only {\n"
                "    bool operator==(const Only& rhs) const { return true; }\n"
                "};\n"
                "template <typename T>\n"
                "bool same(T a, T b) { return a == b; }\n"
            )
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert _operator_calls_from(edges, f"{project}.keep.same") == []


def test_pointer_operands_bind_no_operator(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `Node* p` is typed Node for `p->m()` dispatch, but `p == nullptr`,
    # `++p`, `!p` and `arr + 1` are built-in pointer operations.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "node.hpp": (
                "struct Node {\n"
                "    bool operator==(const Node& o) const { return true; }\n"
                "    bool operator!() const { return false; }\n"
                "    Node& operator++() { return *this; }\n"
                "    Node operator+(int n) const { return *this; }\n"
                "};\n"
                "bool walk(Node* p, Node** pp) {\n"
                "    Node arr[2];\n"
                "    Node* q = nullptr;\n"
                "    ++p;\n"
                "    Node* r = arr + 1;\n"
                "    return p == nullptr || !q || *pp == nullptr;\n"
                "}\n"
            )
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert _operator_calls_from(edges, f"{project}.node.walk") == []


# --- what must keep binding ------------------------------------------------


def test_class_typed_operands_still_bind_their_overload(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]],
) -> None:
    project, edges = issue_repo_calls
    money = f"{project}.money.Money"
    assert _operator_calls_from(edges, f"{project}.util.sum") == [
        (f"{money}.operator_plus", cs.EdgeResolution.EXACT)
    ]
    # A const-reference parameter is the class type, not a pointer.
    assert _operator_calls_from(edges, f"{project}.util.before") == [
        (f"{money}.operator_less", cs.EdgeResolution.EXACT)
    ]


def test_dereferenced_this_binds_the_enclosing_class_overload(
    issue_repo_calls: tuple[str, list[tuple[str, str, str]]],
) -> None:
    # `!(*this == o)`: the `==` binds Money::operator==, the `!` on its
    # bool result binds nothing.
    project, edges = issue_repo_calls
    money = f"{project}.money.Money"
    assert _operator_calls_from(edges, f"{money}.operator_not_equal") == [
        (f"{money}.operator_equal", cs.EdgeResolution.EXACT)
    ]


def test_dereferenced_pointer_binds_the_pointee_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "cmp.hpp": (
                "struct Aaa {\n"
                "    bool operator<(const Aaa& o) const { return true; }\n"
                "};\n"
                "struct Key {\n"
                "    bool operator<(const Key& o) const { return true; }\n"
                "};\n"
                "bool by_key(const Key* a, const Key* b) { return *a < *b; }\n"
            )
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert _operator_calls_from(edges, f"{project}.cmp.by_key") == [
        (f"{project}.cmp.Key.operator_less", cs.EdgeResolution.EXACT)
    ]


def test_right_operand_binds_a_free_overload_beside_its_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `os << m` and `2 * v`: the left operand is no project class, the right
    # one is, and the free overload taking it lives beside that class.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "geo.hpp": (
                "#include <ostream>\n"
                "namespace geo {\n"
                "struct Aaa {};\n"
                "std::ostream& operator<<(std::ostream& os, const Aaa& a);\n"
                "struct Vec { int x; };\n"
                "std::ostream& operator<<(std::ostream& os, const Vec& v) {\n"
                "    return os;\n"
                "}\n"
                "Vec operator*(int k, const Vec& v) { return v; }\n"
                "void show(std::ostream& os, Vec v) {\n"
                "    os << v;\n"
                "    Vec w = 2 * v;\n"
                "}\n"
                "}\n"
            )
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert sorted(_operator_calls_from(edges, f"{project}.geo.geo.show")) == [
        (f"{project}.geo.geo.operator_left_shift", cs.EdgeResolution.EXACT),
        (f"{project}.geo.geo.operator_multiply", cs.EdgeResolution.EXACT),
    ]


def test_right_operand_does_not_bind_its_member_overload(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `5 + m` can never call the MEMBER Money::operator+: a member operator
    # takes its class as the left operand.
    _index(
        temp_repo,
        mock_ingestor,
        {
            "money.h": MONEY_H,
            "mix.cpp": (
                '#include "money.h"\n'
                "Money shift(Money m) {\n"
                "    long k = 5;\n"
                "    k + m.cents;\n"
                "    return m;\n"
                "}\n"
                "bool lhs_int(int k, Money m) { return k < m; }\n"
            ),
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    assert _operator_calls_from(edges, f"{project}.mix.shift") == []
    assert _operator_calls_from(edges, f"{project}.mix.lhs_int") == []


def test_non_operator_calls_in_the_same_body_still_bind(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    _index(
        temp_repo,
        mock_ingestor,
        {
            "money.h": MONEY_H,
            "calc.cpp": (
                '#include "money.h"\n'
                "int twice(int v) { return v * 2; }\n"
                "int calc(int a, int b) { return twice(a + b); }\n"
            ),
        },
    )
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    from_calc = [(dst, res) for src, dst, res in edges if src == f"{project}.calc.calc"]
    assert (f"{project}.calc.twice", cs.EdgeResolution.EXACT) in from_calc


# --- a free operator must be able to take the operand ----------------------


def _operator_edges(
    temp_repo: Path, mock_ingestor: MagicMock, source: str
) -> dict[str, list[tuple[str, str]]]:
    _index(temp_repo, mock_ingestor, {"ops.hpp": source})
    project = temp_repo.name
    edges = _calls(mock_ingestor)
    prefix = f"{project}.ops.g."
    return {
        src.removeprefix(prefix): sorted(
            (callee.removeprefix(prefix), resolution)
            for callee, resolution in _operator_calls_from(edges, src)
        )
        for src in {src for src, _, _ in edges}
        if src.startswith(prefix)
    }


def test_unrelated_free_operator_beside_the_class_binds_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `1 + v` and `v + 1` convert V to int and use built-in `+`; the only
    # operator+ beside V takes two W, so neither can call it.
    calls = _operator_edges(
        temp_repo,
        mock_ingestor,
        "namespace g {\n"
        "struct V { operator int() const { return 1; } };\n"
        "struct W {};\n"
        "W operator+(W, W);\n"
        "int calc(V v) { return 1 + v; }\n"
        "int calc_left(V v) { return v + 1; }\n"
        "}\n",
    )
    assert calls.get("calc", []) == []
    assert calls.get("calc_left", []) == []


def test_free_operator_of_another_arity_binds_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A binary operator- cannot serve unary `-v`, and a postfix
    # operator++(V&, int) cannot serve prefix `++v`.
    calls = _operator_edges(
        temp_repo,
        mock_ingestor,
        "namespace g {\n"
        "struct V { int x; operator int() const { return x; } };\n"
        "V operator-(const V& a, const V& b) { return a; }\n"
        "V operator++(V& v, int) { return v; }\n"
        "int negate(V v) { return -v; }\n"
        "int pre(V v) { ++v; return 0; }\n"
        "int post(V v) { v++; return 0; }\n"
        "}\n",
    )
    assert calls.get("negate", []) == []
    assert calls.get("pre", []) == []
    assert calls["post"] == [("operator_increment", cs.EdgeResolution.EXACT)]


def test_free_operator_taking_the_class_still_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A prototype, a unary overload, a base-class parameter and a template
    # parameter all accept the operand.
    calls = _operator_edges(
        temp_repo,
        mock_ingestor,
        "namespace g {\n"
        "struct Base {};\n"
        "struct V : Base { int x; };\n"
        "V operator+(const V& a, const V& b);\n"
        "V operator-(const V& v) { return v; }\n"
        "bool operator==(const Base& a, const Base& b) { return true; }\n"
        "template <typename T>\n"
        "bool operator!=(const T& a, const T& b) { return false; }\n"
        "void use(V v, V w) {\n"
        "    V s = v + w;\n"
        "    V n = -v;\n"
        "    bool e = v == w;\n"
        "    bool d = v != w;\n"
        "}\n"
        "}\n",
    )
    assert calls["use"] == [
        ("operator_equal", cs.EdgeResolution.EXACT),
        ("operator_minus", cs.EdgeResolution.EXACT),
        ("operator_not_equal", cs.EdgeResolution.EXACT),
        ("operator_plus", cs.EdgeResolution.EXACT),
    ]
