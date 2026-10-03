# C++ member-function overloads used to collapse into one Method node: the
# in-class and out-of-class paths both keyed a member by `Class.name` alone,
# so every overload MERGEd onto the same qualified name and only the last one
# ingested survived (issue #2455). Free-function overloads already got
# `@<line>` variants. A member overload is now one node per signature: the
# first overload keeps the plain name, a later one takes `@<line>` of where it
# is first declared, and an in-class declaration and its out-of-class
# definition still share one node.
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.function_registry import FunctionRegistryTrie
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.cpp_frontend import cpp_frontend_available, run_cpp_frontend
from codebase_rag.tests.conftest import (
    _MockIngestor,
    get_nodes,
    get_qualified_names,
    get_relationships,
    run_updater,
)
from codebase_rag.types_defs import NodeType, OverloadSignature
from codebase_rag.utils.cpp_signatures import (
    ClassLookups,
    parameter_types_may_match,
    signature_arity,
)
from evals.cgr_graph import _StatefulIngestor

PROJECT = "ovl"

INL_CPP = """class Inline {
public:
  int f(int a) { return a; }
  int f(double a) { return (int)a; }
};
int use_inline() { Inline i; return i.f(1) + i.f(2.0); }
"""

SEP_H = """class Sep {
public:
  int g(int a);
  int g(double a);
};
"""

SEP_CPP = """#include "sep.h"
int Sep::g(int a) { return a; }
int Sep::g(double a) { return (int)a; }
int use_sep() { Sep s; return s.g(1) + s.g(2.0); }
"""

FREE_CPP = """int h(int a) { return a; }
int h(double a) { return (int)a; }
int use_free() { return h(1) + h(2.0); }
"""


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _index(root: Path) -> _MockIngestor:
    ingestor = _MockIngestor()
    run_updater(root, ingestor, skip_if_missing=cs.SupportedLanguage.CPP.value)
    return ingestor


def _final_methods(ingestor: _MockIngestor, suffix: str) -> dict[str, dict]:
    # A node write MERGEs on qualified_name and SETs its properties, so the
    # graph holds the LAST write per qn: a declaration and its definition are
    # one node whose location is the definition's.
    final: dict[str, dict] = {}
    for call in ingestor.ensure_node_batch.call_args_list:
        if str(call.args[0]) != cs.NodeLabel.METHOD.value:
            continue
        props = call.args[1]
        qn = props[cs.KEY_QUALIFIED_NAME]
        if qn.endswith(suffix) or qn.rsplit(cs.DUP_QN_MARKER, 1)[0].endswith(suffix):
            final[qn] = props
    return final


def _calls(ingestor: _MockIngestor) -> list[tuple[str, str, object]]:
    edges = []
    for call in get_relationships(ingestor, cs.RelationshipType.CALLS.value):
        props = call.args[3] if len(call.args) > 3 else call.kwargs.get("properties")
        props = props or {}
        edges.append(
            (str(call.args[0][2]), str(call.args[2][2]), props.get(cs.KEY_RESOLUTION))
        )
    return edges


def _overrides(ingestor: _MockIngestor) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(ingestor, cs.RelationshipType.OVERRIDES.value)
    }


def _defines_method(ingestor: _MockIngestor) -> set[tuple[str, str]]:
    return {
        (str(call.args[0][2]), str(call.args[2][2]))
        for call in get_relationships(
            ingestor, cs.RelationshipType.DEFINES_METHOD.value
        )
    }


# --- red: overloads collapse on main -------------------------------------


def test_in_class_overloads_get_one_node_each(temp_repo: Path) -> None:
    root = _write(temp_repo / PROJECT, {"inl.cpp": INL_CPP})
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Inline.f")
    assert set(methods) == {f"{PROJECT}.inl.Inline.f", f"{PROJECT}.inl.Inline.f@4"}
    assert methods[f"{PROJECT}.inl.Inline.f"][cs.KEY_START_LINE] == 3
    assert methods[f"{PROJECT}.inl.Inline.f@4"][cs.KEY_START_LINE] == 4
    assert {m[cs.KEY_NAME] for m in methods.values()} == {"f"}
    assert _defines_method(ingestor) >= {
        (f"{PROJECT}.inl.Inline", f"{PROJECT}.inl.Inline.f"),
        (f"{PROJECT}.inl.Inline", f"{PROJECT}.inl.Inline.f@4"),
    }


def test_out_of_class_overloads_pair_each_declaration_with_its_definition(
    temp_repo: Path,
) -> None:
    root = _write(temp_repo / PROJECT, {"sep.h": SEP_H, "sep.cpp": SEP_CPP})
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Sep.g")
    # Two overloads, two nodes: neither the header declarations nor the
    # source definitions mint a node of their own.
    plain = f"{PROJECT}.sep.h.Sep.g"
    variant = f"{PROJECT}.sep.h.Sep.g@4"
    assert set(methods) == {plain, variant}
    assert (methods[plain][cs.KEY_PATH], methods[plain][cs.KEY_START_LINE]) == (
        "sep.cpp",
        2,
    )
    assert (methods[variant][cs.KEY_PATH], methods[variant][cs.KEY_START_LINE]) == (
        "sep.cpp",
        3,
    )


def test_calls_to_an_overloaded_method_fan_out_as_overload(temp_repo: Path) -> None:
    root = _write(
        temp_repo / PROJECT,
        {"inl.cpp": INL_CPP, "sep.h": SEP_H, "sep.cpp": SEP_CPP},
    )
    ingestor = _index(root)
    calls = _calls(ingestor)

    for caller, natural in (
        (f"{PROJECT}.inl.use_inline", f"{PROJECT}.inl.Inline.f"),
        (f"{PROJECT}.sep.use_sep", f"{PROJECT}.sep.h.Sep.g"),
    ):
        edges = {(dst, res) for src, dst, res in calls if src == caller}
        targets = {dst for dst, _ in edges}
        assert targets == {natural, f"{natural}@4"}, edges
        assert {res for _, res in edges} == {cs.EdgeResolution.OVERLOAD}, edges


def test_calls_inside_each_overload_body_belong_to_that_overload(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "body.cpp": (
                "int helper_a() { return 1; }\n"
                "int helper_b() { return 2; }\n"
                "class Body {\n"
                "public:\n"
                "  int run(int a) { return helper_a(); }\n"
                "  int run(double a) { return helper_b(); }\n"
                "  int out(int a);\n"
                "  int out(double a);\n"
                "};\n"
                "int Body::out(int a) { return helper_a(); }\n"
                "int Body::out(double a) { return helper_b(); }\n"
            )
        },
    )
    ingestor = _index(root)
    sources = {}
    for src, dst, _ in _calls(ingestor):
        sources.setdefault(dst.rsplit(".", 1)[-1], set()).add(src)

    body = f"{PROJECT}.body.Body"
    assert sources["helper_a"] == {f"{body}.run", f"{body}.out"}
    assert sources["helper_b"] == {f"{body}.run@6", f"{body}.out@8"}


def test_const_and_non_const_overloads_are_two_nodes(temp_repo: Path) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "vec.h": (
                "class Vec {\n"
                "public:\n"
                "  int get(int i);\n"
                "  int get(int i) const;\n"
                "  int& at(int i);\n"
                "  const int& at(int i) const;\n"
                "  int d[4];\n"
                "};\n"
            ),
            "vec.cpp": (
                '#include "vec.h"\n'
                "int Vec::get(int i) { return d[i]; }\n"
                "int Vec::get(int i) const { return d[i]; }\n"
                "int& Vec::at(int i) { return d[i]; }\n"
                "const int& Vec::at(int i) const { return d[i]; }\n"
            ),
        },
    )
    ingestor = _index(root)

    get = _final_methods(ingestor, ".Vec.get")
    assert set(get) == {f"{PROJECT}.vec.h.Vec.get", f"{PROJECT}.vec.h.Vec.get@4"}
    assert get[f"{PROJECT}.vec.h.Vec.get@4"][cs.KEY_SIGNATURE] == "(int) const"
    # A declaration returning a reference is not ingested from the class body,
    # so the definitions alone name these; still one node per overload.
    at = _final_methods(ingestor, ".Vec.at")
    assert len(at) == 2 and f"{PROJECT}.vec.h.Vec.at" in at, at
    assert {m[cs.KEY_START_LINE] for m in at.values()} == {4, 5}


def test_overloaded_constructors_are_one_node_each(temp_repo: Path) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "w.cpp": (
                "class W {\n"
                "public:\n"
                "  W();\n"
                "  W(int n);\n"
                "  W(const W& other);\n"
                "};\n"
                "W::W() {}\n"
                "W::W(int n) {}\n"
                "W::W(const W& other) {}\n"
            )
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".W.W")
    assert set(methods) == {
        f"{PROJECT}.w.W.W",
        f"{PROJECT}.w.W.W@4",
        f"{PROJECT}.w.W.W@5",
    }
    assert {m[cs.KEY_START_LINE] for m in methods.values()} == {7, 8, 9}


def test_an_override_links_to_the_base_overload_of_its_signature(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "ovr.cpp": (
                "class Base {\n"
                "public:\n"
                "  virtual int f(int a) { return a; }\n"
                "  virtual int f(double a) { return 0; }\n"
                "};\n"
                "class One : public Base {\n"
                "public:\n"
                "  int f(double a) override { return 1; }\n"
                "};\n"
                "class Both : public Base {\n"
                "public:\n"
                "  int f(int a) override { return 2; }\n"
                "  int f(double a) override { return 3; }\n"
                "};\n"
            )
        },
    )
    ingestor = _index(root)

    base = f"{PROJECT}.ovr.Base"
    assert {
        edge for edge in _overrides(ingestor) if edge[1].startswith(f"{base}.f")
    } == {
        (f"{PROJECT}.ovr.One.f", f"{base}.f@4"),
        (f"{PROJECT}.ovr.Both.f", f"{base}.f"),
        (f"{PROJECT}.ovr.Both.f@13", f"{base}.f@4"),
    }


def test_overloads_on_same_named_types_of_two_namespaces_stay_apart(
    temp_repo: Path,
) -> None:
    # The namespace is what tells these parameter types apart, so it stays in
    # the signature: stripped, both read `(T)` and the second overload
    # overwrote the first.
    root = _write(
        temp_repo / PROJECT,
        {
            "qual.h": (
                "namespace a { struct T {}; }\n"
                "namespace b { struct T {}; }\n"
                "class Q {\n"
                "public:\n"
                "  void f(a::T x);\n"
                "  void f(b::T x);\n"
                "};\n"
            ),
            "qual.cpp": (
                '#include "qual.h"\nvoid Q::f(a::T x) {}\nvoid Q::f(b::T x) {}\n'
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Q.f")
    plain = f"{PROJECT}.qual.h.Q.f"
    variant = f"{PROJECT}.qual.h.Q.f@6"
    assert set(methods) == {plain, variant}
    assert (methods[plain][cs.KEY_START_LINE], methods[plain][cs.KEY_SIGNATURE]) == (
        2,
        "(a::T)",
    )
    assert (
        methods[variant][cs.KEY_START_LINE],
        methods[variant][cs.KEY_SIGNATURE],
    ) == (3, "(b::T)")


OVERRIDE_ALIAS_CPP = """typedef int Alias;
class Base {
public:
  virtual int f(double a) { return 0; }
  virtual int f(int a) { return a; }
  virtual int g(double a) { return 0; }
  virtual int g(int a, int b) { return a; }
};
class Derived : public Base {
public:
  int f(Alias a) override { return 1; }
  int g(Alias a, Alias b) override { return 2; }
};
"""


def test_an_override_matching_no_base_signature_is_not_given_the_plain_name(
    temp_repo: Path,
) -> None:
    # `f(Alias)` overrides `f(int)` through a typedef the parser cannot see
    # through, so neither base `f` matches it, and both take one argument:
    # nothing tells them apart, so no edge beats one onto `f(double)`, which
    # merely holds the plain name. `g(Alias, Alias)` has one base overload of
    # its arity, and that one it overrides.
    root = _write(temp_repo / PROJECT, {"alias.cpp": OVERRIDE_ALIAS_CPP})
    ingestor = _index(root)

    edges = {
        edge
        for edge in _overrides(ingestor)
        if edge[0].startswith(f"{PROJECT}.alias.Derived.")
    }
    assert edges == {
        (f"{PROJECT}.alias.Derived.g", f"{PROJECT}.alias.Base.g@7"),
    }


def test_an_override_of_another_arity_is_not_given_the_lone_base_overload(
    temp_repo: Path,
) -> None:
    # One base `f`, so nothing to confuse it with, but `f(int, int)` takes two
    # arguments where it takes one: a hiding member, not an override.
    root = _write(
        temp_repo / PROJECT,
        {
            "hide.cpp": (
                "class B1 { public: virtual int f(int a) { return a; } };\n"
                "class D1 : public B1 { public: int f(int a, int b) { return 0; } };\n"
            )
        },
    )
    ingestor = _index(root)

    assert not {
        edge for edge in _overrides(ingestor) if edge[0] == f"{PROJECT}.hide.D1.f"
    }


HIDE_TYPES_CPP = """class Foo {};
class Bar {};
class HB {
public:
  virtual int f(int a) { return a; }
  virtual int g(Foo x) { return 0; }
};
class HD : public HB {
public:
  int f(double a) { return 0; }
  int g(Bar x) { return 1; }
};
class HC : public HB {
public:
  int f(const char* s) { return 0; }
};
"""


def test_a_member_of_other_parameter_types_hides_the_lone_base_overload(
    temp_repo: Path,
) -> None:
    # Same arity, but `double`, `int` and `const char*` are built-in types and
    # `Foo` and `Bar` are two classes the graph holds: no typedef can make
    # one the other, so these members hide the base's, they do not override.
    root = _write(temp_repo / PROJECT, {"hide.cpp": HIDE_TYPES_CPP})
    ingestor = _index(root)

    derived = {f"{PROJECT}.hide.HD.f", f"{PROJECT}.hide.HD.g", f"{PROJECT}.hide.HC.f"}
    assert {edge for edge in _overrides(ingestor) if edge[0] in derived} == set()


TEMPLATE_HIDE_CPP = """#include <vector>
#include <map>
#include <cstddef>
class Foo {};
class Bar {};
class TB {
public:
  virtual int v(std::vector<int> x) { return 0; }
  virtual int m(std::map<int, Foo> x) { return 0; }
};
class TD : public TB {
public:
  int v(std::vector<double> x) { return 1; }
  int m(std::map<int, Bar> x) { return 1; }
};
"""


def test_a_member_taking_another_instance_of_a_template_hides_the_base_one(
    temp_repo: Path,
) -> None:
    # `std::vector` is the same template on both sides, so its arguments
    # decide: `int` is not `double`, and `Foo` and `Bar` are two classes.
    root = _write(temp_repo / PROJECT, {"tpl.cpp": TEMPLATE_HIDE_CPP})
    ingestor = _index(root)

    assert {
        edge
        for edge in _overrides(ingestor)
        if edge[0].startswith(f"{PROJECT}.tpl.TD.")
    } == set()


ALIAS_OVERRIDE_CPP = """#include <cstddef>
#include <vector>
typedef int Alias;
class AB {
public:
  virtual int s(std::size_t n) { return 0; }
  virtual int u(size_t n) { return 0; }
  virtual int w(std::vector<int> x) { return 0; }
  virtual int c(int n) { return 0; }
};
class AD : public AB {
public:
  int s(unsigned long n) override { return 1; }
  int u(unsigned int n) override { return 1; }
  int w(std::vector<Alias> x) override { return 1; }
  int c(const int n) override { return 1; }
};
"""


def test_an_override_through_a_platform_or_template_alias_still_links(
    temp_repo: Path,
) -> None:
    # `size_t` is an implementation-defined alias of an unsigned type, so it
    # may be `unsigned long` or `unsigned int`; `vector<Alias>` may be
    # `vector<int>`; and a by-value parameter's own `const` is no part of a
    # function's type.
    root = _write(temp_repo / PROJECT, {"alias.cpp": ALIAS_OVERRIDE_CPP})
    ingestor = _index(root)

    base = f"{PROJECT}.alias.AB"
    derived = f"{PROJECT}.alias.AD"
    assert {
        edge for edge in _overrides(ingestor) if edge[0].startswith(f"{derived}.")
    } == {(f"{derived}.{name}", f"{base}.{name}") for name in ("s", "u", "w", "c")}


def test_a_by_value_parameters_own_const_is_not_part_of_the_signature(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "cv.h": (
                "class Cv {\npublic:\n  void f(int n);\n  void f(const char* s);\n};\n"
            ),
            "cv.cpp": (
                '#include "cv.h"\n'
                "void Cv::f(const int n) {}\n"
                "void Cv::f(const char* const s) {}\n"
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Cv.f")
    assert {
        qn: (m[cs.KEY_START_LINE], m[cs.KEY_SIGNATURE]) for qn, m in methods.items()
    } == {
        f"{PROJECT}.cv.h.Cv.f": (2, "(int)"),
        f"{PROJECT}.cv.h.Cv.f@4": (3, "(const char*)"),
    }


DECLARATOR_OVERRIDE_CPP = """typedef int Alias;
class Foo {};
class DB {
public:
  virtual int p(int a) { return 0; }
  virtual int q(int* a) { return 0; }
  virtual int v(const Foo a) { return 0; }
  virtual int r(const Foo& a) { return 0; }
};
class DD : public DB {
public:
  int p(Alias* a) { return 1; }
  int q(Alias* a) override { return 1; }
  int v(Foo a) override { return 1; }
  int r(Foo& a) { return 1; }
};
"""


def test_an_override_compares_pointers_references_and_pointee_const_exactly(
    temp_repo: Path,
) -> None:
    # Only the base type may hide behind an alias: `Alias*` is a pointer and
    # `int` is not, while `Alias*` may be `int*`. A by-value parameter's own
    # `const` is no part of the type, so `v(Foo)` overrides `v(const Foo)`;
    # the `const` a reference refers to is, so `r(Foo&)` hides
    # `r(const Foo&)`.
    root = _write(temp_repo / PROJECT, {"decl.cpp": DECLARATOR_OVERRIDE_CPP})
    ingestor = _index(root)

    base = f"{PROJECT}.decl.DB"
    derived = f"{PROJECT}.decl.DD"
    assert {
        edge for edge in _overrides(ingestor) if edge[0].startswith(f"{derived}.")
    } == {(f"{derived}.q", f"{base}.q"), (f"{derived}.v", f"{base}.v")}


SCOPED_ALIAS_CPP = """namespace other { class Foo {}; }
namespace ns {
typedef int Foo;
class SB {
public:
  virtual int f(int a) { return 0; }
};
class SD : public SB {
public:
  int f(Foo a) override { return 1; }
};
}
"""


def test_an_unrelated_class_of_the_same_name_does_not_mask_an_alias(
    temp_repo: Path,
) -> None:
    # Inside `ns`, `Foo` is the typedef for `int`; `other::Foo` is a class,
    # but not one `SD` can name as plain `Foo`, so it must not decide that
    # `f(Foo)` cannot override `f(int)`.
    root = _write(temp_repo / PROJECT, {"scoped.cpp": SCOPED_ALIAS_CPP})
    ingestor = _index(root)

    assert (
        f"{PROJECT}.scoped.ns.SD.f",
        f"{PROJECT}.scoped.ns.SB.f",
    ) in _overrides(ingestor)


@pytest.mark.parametrize(
    ("left", "right", "may_match"),
    [
        ("(int)", "(Alias)", True),
        ("(const std::string&)", "(const string&)", True),
        ("(int,Count)", "(int,int)", True),
        ("(int)", "(double)", False),
        ("(int)", "(const char*)", False),
        # `size_t` is whatever unsigned type the platform says: an alias.
        ("(std::size_t)", "(unsigned long)", True),
        ("(size_t)", "(unsigned int)", True),
        ("(std::nullptr_t)", "(int)", False),
        ("(Foo)", "(Bar)", False),
        ("(int) const", "(int)", False),
        # Same template, so the arguments decide, by the same rules.
        ("(std::vector<int>)", "(std::vector<double>)", False),
        ("(std::map<int,Foo>)", "(std::map<int,Bar>)", False),
        ("(std::vector<Alias>)", "(std::vector<int>)", True),
        ("(std::vector<T>)", "(std::vector<U>)", True),
        ("(const std::vector<int>&)", "(const std::vector<int>&)", True),
        # Another template name may be an alias template.
        ("(IntVec<int>)", "(std::vector<int>)", True),
        # `std::string` is `basic_string<char>`, never a built-in.
        ("(std::string)", "(int)", False),
        ("(std::string)", "(std::basic_string<char>)", True),
        ("(std::function<int(int,int)>)", "(std::function<int(int,int)>)", True),
        # Only the base type may be an alias; the declarators must agree,
        # unless a bare alias on one side stands for the whole other type.
        ("(int)", "(Alias*)", False),
        ("(int*)", "(Alias*)", True),
        ("(const char*)", "(PtrT)", True),
        ("(const Foo&)", "(Foo&)", False),
        ("(const Foo)", "(Foo)", True),
        ("(Foo*)", "(Foo&)", False),
    ],
)
def test_parameter_types_may_match_only_through_a_possible_alias(
    left: str, right: str, may_match: bool
) -> None:
    def known_class(name: str) -> bool:
        return name in {"Foo", "Bar"}

    classes = ClassLookups(left=known_class, right=known_class)
    assert parameter_types_may_match(left, right, classes) is may_match


def test_a_definition_of_another_arity_is_not_paired_with_the_lone_declaration() -> (
    None
):
    registry = FunctionRegistryTrie()
    qn = registry.register_overload_qn(
        "p.C.f", OverloadSignature("(int)", 1), 3, declared_in_class=True
    )
    registry[qn] = NodeType.METHOD
    assert (
        registry.register_overload_qn("p.C.f", OverloadSignature("(int,int)", 2), 9)
        == "p.C.f@9"
    )


# --- negative: what must not change --------------------------------------


def test_non_overloaded_methods_keep_their_plain_name(temp_repo: Path) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "solo.h": (
                "class Solo {\n"
                "public:\n"
                "  void run();\n"
                "  int size() const { return 0; }\n"
                "};\n"
            ),
            "solo.cpp": (
                '#include "solo.h"\n'
                "void Solo::run() {}\n"
                "int use_solo() { Solo s; s.run(); return s.size(); }\n"
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, "")
    solo = {qn for qn in methods if ".Solo." in qn}
    assert solo == {f"{PROJECT}.solo.h.Solo.run", f"{PROJECT}.solo.h.Solo.size"}
    assert methods[f"{PROJECT}.solo.h.Solo.run"][cs.KEY_PATH] == "solo.cpp"
    calls = {
        (dst, res)
        for src, dst, res in _calls(ingestor)
        if src == f"{PROJECT}.solo.use_solo"
    }
    assert calls == {
        (f"{PROJECT}.solo.h.Solo.run", cs.EdgeResolution.EXACT),
        (f"{PROJECT}.solo.h.Solo.size", cs.EdgeResolution.EXACT),
    }


def test_definition_spelled_differently_from_its_declaration_stays_one_node(
    temp_repo: Path,
) -> None:
    # Parameter names, default arguments, namespace qualification and spacing
    # are not part of an overload's identity: the declaration and the
    # definition below are one member.
    root = _write(
        temp_repo / PROJECT,
        {
            "cfg.h": (
                "#include <string>\n"
                "class Cfg {\n"
                "public:\n"
                "  void set(const std::string& value, int n = 0);\n"
                "};\n"
            ),
            "cfg.cpp": (
                '#include "cfg.h"\n'
                "using namespace std;\n"
                "void Cfg::set(const string &v, int count) {}\n"
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Cfg.set")
    assert set(methods) == {f"{PROJECT}.cfg.h.Cfg.set"}
    assert methods[f"{PROJECT}.cfg.h.Cfg.set"][cs.KEY_PATH] == "cfg.cpp"


def test_declaration_unqualified_in_its_namespace_pairs_with_a_qualified_definition(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "ns.h": (
                "namespace a {\n"
                "struct T {};\n"
                "class N {\n"
                "public:\n"
                "  void f(T x);\n"
                "  void f(int x);\n"
                "};\n"
                "}\n"
            ),
            "ns.cpp": (
                '#include "ns.h"\nvoid a::N::f(a::T x) {}\nvoid a::N::f(int x) {}\n'
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".N.f")
    assert len(methods) == 2, methods
    assert {(m[cs.KEY_PATH], m[cs.KEY_START_LINE]) for m in methods.values()} == {
        ("ns.cpp", 2),
        ("ns.cpp", 3),
    }


def test_respelled_definitions_of_an_overloaded_member_pair_with_their_declarations(
    temp_repo: Path,
) -> None:
    # Overloaded, so no lone declaration to fall back on: `string` pairs with
    # `std::string` because the spellings agree up to qualification, and the
    # default argument is not part of the type.
    root = _write(
        temp_repo / PROJECT,
        {
            "kv.h": (
                "#include <string>\n"
                "class Kv {\n"
                "public:\n"
                "  void put(const std::string& key, int ttl = 0);\n"
                "  void put(int slot);\n"
                "};\n"
            ),
            "kv.cpp": (
                '#include "kv.h"\n'
                "using namespace std;\n"
                "void Kv::put(int slot) {}\n"
                "void Kv::put(const string &k, int t) {}\n"
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".Kv.put")
    assert {
        qn: (m[cs.KEY_PATH], m[cs.KEY_START_LINE]) for qn, m in methods.items()
    } == {
        f"{PROJECT}.kv.h.Kv.put": ("kv.cpp", 4),
        f"{PROJECT}.kv.h.Kv.put@5": ("kv.cpp", 3),
    }


@pytest.mark.parametrize(
    ("text", "arity"),
    [
        ("()", 0),
        ("(int)", 1),
        ("(std::map<int,int>,int) const", 2),
        ("(int(*)(int,int),double)", 2),
        ("(const char*,...) &&", 2),
    ],
)
def test_arity_read_back_from_a_stored_signature(text: str, arity: int) -> None:
    assert signature_arity(text) == arity


def test_an_override_spelled_through_a_typedef_still_links_to_the_lone_base(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "tdef.cpp": (
                "typedef int Alias;\n"
                "class B2 { public: virtual int f(int a) { return a; } };\n"
                "class D2 : public B2 { public: int f(Alias a) override { return 0; } };\n"
            )
        },
    )
    ingestor = _index(root)

    assert (f"{PROJECT}.tdef.D2.f", f"{PROJECT}.tdef.B2.f") in _overrides(ingestor)


def test_a_typedef_respelled_definition_of_a_lone_member_stays_one_node(
    temp_repo: Path,
) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "one.h": (
                "typedef int Count;\n"
                "class One {\n"
                "public:\n"
                "  int f(Count n, int base = 0);\n"
                "};\n"
            ),
            "one.cpp": (
                '#include "one.h"\nint One::f(int n, int base) { return n; }\n'
            ),
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".One.f")
    assert set(methods) == {f"{PROJECT}.one.h.One.f"}
    assert methods[f"{PROJECT}.one.h.One.f"][cs.KEY_PATH] == "one.cpp"


def test_same_name_in_different_classes_is_not_an_overload(temp_repo: Path) -> None:
    root = _write(
        temp_repo / PROJECT,
        {
            "two.cpp": (
                "class A { public: virtual int run(int a) { return a; } };\n"
                "class B { public: int run(int a) { return a; } };\n"
                "class C : public A { public: int run(int a) override { return 0; } };\n"
            )
        },
    )
    ingestor = _index(root)

    methods = _final_methods(ingestor, ".run")
    assert set(methods) == {
        f"{PROJECT}.two.A.run",
        f"{PROJECT}.two.B.run",
        f"{PROJECT}.two.C.run",
    }
    # A base that is not overloaded is still overridden by name.
    assert (f"{PROJECT}.two.C.run", f"{PROJECT}.two.A.run") in _overrides(ingestor)


def test_free_function_overloads_are_unchanged(temp_repo: Path) -> None:
    root = _write(temp_repo / PROJECT, {"free.cpp": FREE_CPP})
    ingestor = _index(root)

    functions = {
        call.args[1][cs.KEY_QUALIFIED_NAME]
        for call in ingestor.ensure_node_batch.call_args_list
        if str(call.args[0]) == cs.NodeLabel.FUNCTION.value
    }
    assert functions == {
        f"{PROJECT}.free.h",
        f"{PROJECT}.free.h@2",
        f"{PROJECT}.free.use_free",
    }


def test_a_member_from_a_graph_without_signatures_stays_one_node() -> None:
    # An incremental run over a graph written before this change reads the
    # member back with no signature: it keeps standing for every overload
    # instead of a re-parsed definition splitting off a node beside it.
    registry = FunctionRegistryTrie()
    registry["p.h.Sep.g"] = NodeType.METHOD
    for line, text in ((2, "(int)"), (3, "(double)")):
        qn = registry.register_overload_qn(
            "p.h.Sep.g", OverloadSignature(text, 1), line
        )
        assert qn == "p.h.Sep.g"
    assert registry.overload_signature("p.h.Sep.g") is None


def test_a_definition_matching_no_declaration_is_not_guessed_onto_one() -> None:
    registry = FunctionRegistryTrie()
    for line, text, arity in ((3, "(int)", 1), (4, "(double)", 1), (5, "(int,int)", 2)):
        qn = registry.register_overload_qn(
            "p.C.f", OverloadSignature(text, arity), line, declared_in_class=True
        )
        registry[qn] = NodeType.METHOD
    assert registry.variants("p.C.f") == ["p.C.f", "p.C.f@4", "p.C.f@5"]

    # Two declarations take one argument: neither is guessed.
    assert (
        registry.register_overload_qn("p.C.f", OverloadSignature("(long)", 1), 9)
        == "p.C.f@9"
    )
    # Only one takes two: a respelled definition of it pairs with it.
    assert (
        registry.register_overload_qn("p.C.f", OverloadSignature("(int,long)", 2), 11)
        == "p.C.f@5"
    )
    # A deregistered overload takes its signature with it.
    del registry["p.C.f@4"]
    assert registry.overload_signature("p.C.f@4") is None
    assert "p.C.f@4" not in registry.variants("p.C.f")


# --- incremental: a re-parse lands on the clean index's nodes --------------

# The definitions swapped and moved down a line: only the signature can pair
# each with the overload its unchanged header declares.
SEP_CPP_EDITED = """#include "sep.h"

int Sep::g(double a) { return (int)a; }
int Sep::g(int a) { return a; }
int use_sep() { Sep s; return s.g(1) + s.g(2.0); }
"""
SEP_H_EDITED = SEP_H + "// touched\n"
EDITS = {"sep.cpp": SEP_CPP_EDITED, "sep.h": SEP_H_EDITED}


def _updater(store: _StatefulIngestor, repo: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CPP not in parsers:
        pytest.skip("cpp parser not available")
    return GraphUpdater(
        ingestor=store,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


def _state(store: _StatefulIngestor, root: Path) -> tuple[frozenset, frozenset, dict]:
    # File nodes are keyed by absolute path; the clean index lives elsewhere.
    def uid(value: object) -> str:
        return str(value).replace(root.resolve().as_posix(), "<root>")

    nodes = frozenset((label, uid(value)) for (label, value) in store.nodes)
    edges = frozenset(
        (str(fl), uid(fv), str(rel), str(tl), uid(tv))
        for (fl, fv, rel, tl, tv) in store.edges
    )
    methods = {
        str(value): (
            props.get(cs.KEY_PATH),
            props.get(cs.KEY_START_LINE),
            props.get(cs.KEY_SIGNATURE),
        )
        for (label, value), props in store.nodes.items()
        if label == cs.NodeLabel.METHOD.value
    }
    return nodes, edges, methods


def _incremental_and_clean(
    temp_repo: Path, files: dict[str, str], edited: str, text: str, mode: str
) -> tuple[tuple, tuple]:
    root = _write(temp_repo / PROJECT, files)
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)

    path = root / edited
    cache_mtime = (root / cs.HASH_CACHE_FILENAME).stat().st_mtime
    path.write_text(text, encoding="utf-8")
    # Past the hash cache's mtime, so a coarse clock cannot skip the file.
    os.utime(path, (cache_mtime + 1, cache_mtime + 1))
    # A FRESH updater each time: its registry holds only what it re-parses
    # and what it reads back from the graph, which is the case that needs the
    # persisted signature.
    if mode == "run":
        _updater(store, root).run(force=False)
    else:
        _updater(store, root).reingest([path])

    clean_root = _write(
        temp_repo / "clean" / PROJECT,
        {rel: (root / rel).read_text() for rel in files},
    )
    clean_store = _StatefulIngestor()
    _updater(clean_store, clean_root).run(force=True)
    return _state(store, root), _state(clean_store, clean_root)


@pytest.mark.parametrize("edited", sorted(EDITS))
@pytest.mark.parametrize("mode", ["run", "reingest"])
def test_incremental_update_matches_a_clean_index(
    temp_repo: Path, edited: str, mode: str
) -> None:
    incremental, clean = _incremental_and_clean(
        temp_repo, {"sep.h": SEP_H, "sep.cpp": SEP_CPP}, edited, EDITS[edited], mode
    )
    assert incremental[2] == clean[2]
    assert {qn for qn in clean[2] if ".Sep.g" in qn} == {
        f"{PROJECT}.sep.h.Sep.g",
        f"{PROJECT}.sep.h.Sep.g@4",
    }
    assert incremental[:2] == clean[:2]


CNT_H = """typedef int Count;
class Cnt {
public:
  int f(Count n);
  int f(double a, double b);
};
"""
CNT_CPP = """#include "cnt.h"
int Cnt::f(Count n) { return n; }
int Cnt::f(double a, double b) { return 0; }
"""
# `Count` respelled as the `int` it aliases: no declaration matches the text,
# and only the arity read back from the graph can pair it with `f(Count)`.
CNT_CPP_EDITED = CNT_CPP.replace("Cnt::f(Count n)", "Cnt::f(int n)")


@pytest.mark.parametrize("mode", ["run", "reingest"])
def test_incremental_respelled_definition_matches_a_clean_index(
    temp_repo: Path, mode: str
) -> None:
    incremental, clean = _incremental_and_clean(
        temp_repo, {"cnt.h": CNT_H, "cnt.cpp": CNT_CPP}, "cnt.cpp", CNT_CPP_EDITED, mode
    )
    assert {qn for qn in clean[2] if ".Cnt.f" in qn} == {
        f"{PROJECT}.cnt.h.Cnt.f",
        f"{PROJECT}.cnt.h.Cnt.f@5",
    }
    assert incremental[2] == clean[2]
    assert incremental[:2] == clean[:2]


# A declaration returning a reference is not read from the class body, so
# these overloads are known only from their definitions. A definition added
# beside the one already indexed is a new overload, not the old one's twin.
VREF_H = """class V {
public:
  int& at(int i);
  int& at(long i);
  int d[4];
};
"""
VREF_CPP = """#include "vref.h"
int& V::at(int i) { return d[i]; }
"""
VREF_CPP_EDITED = VREF_CPP + "int& V::at(long i) { return d[i]; }\n"


@pytest.mark.parametrize("mode", ["run", "reingest"])
def test_incremental_definition_only_overload_matches_a_clean_index(
    temp_repo: Path, mode: str
) -> None:
    incremental, clean = _incremental_and_clean(
        temp_repo,
        {"vref.h": VREF_H, "v.cpp": VREF_CPP},
        "v.cpp",
        VREF_CPP_EDITED,
        mode,
    )
    assert {qn for qn in clean[2] if ".V.at" in qn} == {
        f"{PROJECT}.vref.V.at",
        f"{PROJECT}.vref.V.at@3",
    }
    assert incremental[2] == clean[2]
    assert incremental[:2] == clean[:2]


def test_warm_reingest_of_a_definition_file_keeps_each_overload(
    temp_repo: Path,
) -> None:
    root = _write(temp_repo / PROJECT, {"sep.h": SEP_H, "sep.cpp": SEP_CPP})
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    (root / "sep.cpp").write_text(SEP_CPP_EDITED, encoding="utf-8")
    updater.reingest([root / "sep.cpp"])

    methods = _state(store, root)[2]
    assert {qn: v for qn, v in methods.items() if ".Sep.g" in qn} == {
        f"{PROJECT}.sep.h.Sep.g": ("sep.cpp", 4, "(int)"),
        f"{PROJECT}.sep.h.Sep.g@4": ("sep.cpp", 3, "(double)"),
    }


# --- the issue's setup: a compile_commands.json, so libclang runs ----------

libclang = pytest.mark.skipif(
    not cpp_frontend_available(), reason="libclang not available"
)

# The issue reproduced the collapse "with the libclang frontend enabled": a
# compile_commands.json beside the sources and the default CPP_FRONTEND, which
# is HYBRID. Tree-sitter still names every definition there; libclang adds
# macro nodes and the calls only an expansion shows, joined to tree-sitter's
# spans by location, so those land on the exact overload.
LIBCLANG_SEP_H = SEP_H + "#define CALL_G(s, x) (s).g(x)\n"
LIBCLANG_SEP_CPP = SEP_CPP + (
    "int use_macro_int() { Sep s; return CALL_G(s, 1); }\n"
    "int use_macro_dbl() { Sep s; return CALL_G(s, 2.0); }\n"
)


def _write_compdb(root: Path, sources: tuple[str, ...]) -> None:
    (root / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "arguments": ["c++", "-std=c++17", f"-I{root}", str(root / src)],
                    "file": str(root / src),
                }
                for src in sources
            ]
        ),
        encoding="utf-8",
    )


@libclang
def test_hybrid_libclang_setup_gives_each_overload_its_node(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)
    root = _write(
        temp_repo / PROJECT,
        {"inl.cpp": INL_CPP, "sep.h": LIBCLANG_SEP_H, "sep.cpp": LIBCLANG_SEP_CPP},
    )
    _write_compdb(root, ("inl.cpp", "sep.cpp"))
    ingestor = _index(root)

    # The macro node exists only because libclang ran.
    functions = get_qualified_names(get_nodes(ingestor, cs.NodeLabel.FUNCTION.value))
    assert f"{PROJECT}.sep.h.CALL_G" in functions
    assert set(_final_methods(ingestor, ".Inline.f")) == {
        f"{PROJECT}.inl.Inline.f",
        f"{PROJECT}.inl.Inline.f@4",
    }
    assert set(_final_methods(ingestor, ".Sep.g")) == {
        f"{PROJECT}.sep.h.Sep.g",
        f"{PROJECT}.sep.h.Sep.g@4",
    }
    calls = _calls(ingestor)
    by_caller = {}
    for src, dst, res in calls:
        by_caller.setdefault(src.rsplit(".", 1)[-1], set()).add((dst, res))
    natural = f"{PROJECT}.sep.h.Sep.g"
    assert by_caller["use_sep"] == {
        (natural, cs.EdgeResolution.OVERLOAD),
        (f"{natural}@4", cs.EdgeResolution.OVERLOAD),
    }
    # libclang resolved the overload inside each expansion: one edge each,
    # to the overload the argument selects.
    assert {dst for dst, _ in by_caller["use_macro_int"]} - {
        f"{PROJECT}.sep.h.CALL_G"
    } == {natural}
    assert {dst for dst, _ in by_caller["use_macro_dbl"]} - {
        f"{PROJECT}.sep.h.CALL_G"
    } == {f"{natural}@4"}


# `CPP_FRONTEND=libclang` names definitions itself. It keyed a member by its
# plain qn too, so it collapsed overloads the same way; it now names each one
# exactly as the tree-sitter path does, and binds every call to the overload
# libclang resolved.
PURE_INL_CPP = INL_CPP.replace(
    "int use_inline() { Inline i; return", "int use_inline(Inline& i) { return"
)
PURE_SEP_H = """class Sep {
public:
  int g(int a);
  int g(double a);
  Sep(int n);
  Sep(const Sep& o);
  void k() const;
  void k();
};
"""
PURE_SEP_CPP = """#include "sep.h"
int Sep::g(int a) { return a; }
int Sep::g(double a) { return (int)a; }
Sep::Sep(int n) {}
Sep::Sep(const Sep& o) {}
void Sep::k() const {}
void Sep::k() {}
int use_sep(Sep& s) { s.k(); return s.g(1) + s.g(2.0); }
"""
PURE_BOX_CPP = """template <typename T> class Box {
public:
  void put(T v) { v_ = v; }
  void put(T a, T b) { v_ = a; }
  T v_;
};
int use_box(Box<int>& b) { b.put(1); b.put(1, 2); return 0; }
"""


# Calls that libclang resolves before the walk reaches the overload they
# name: `a()`'s inline body comes before both `later` declarations, and
# `Fwd::b` is defined before either `later` definition.
PURE_FWD_H = """class Fwd {
public:
  void a() { later(1.0); }
  void later(int x);
  void later(double x);
  void b();
};
"""
PURE_FWD_CPP = """#include "fwd.h"
void Fwd::b() { later(2.0); }
void Fwd::later(int x) {}
void Fwd::later(double x) {}
"""


def _pure_libclang_project(root: Path) -> Path:
    _write(
        root,
        {
            "inl.cpp": PURE_INL_CPP,
            "sep.h": PURE_SEP_H,
            "sep.cpp": PURE_SEP_CPP,
            "box.cpp": PURE_BOX_CPP,
            "fwd.h": PURE_FWD_H,
            "fwd.cpp": PURE_FWD_CPP,
        },
    )
    _write_compdb(root, ("inl.cpp", "sep.cpp", "box.cpp", "fwd.cpp"))
    return root


@libclang
def test_pure_libclang_frontend_names_overloads_as_tree_sitter_does(
    temp_repo: Path,
) -> None:
    root = _pure_libclang_project(temp_repo / PROJECT)
    tree_sitter = _index(root)
    frontend = _MockIngestor()
    run_cpp_frontend(frontend, root, root.name, root)

    label = cs.NodeLabel.METHOD.value
    expected = {
        f"{PROJECT}.inl.Inline.f",
        f"{PROJECT}.inl.Inline.f@4",
        f"{PROJECT}.sep.h.Sep.g",
        f"{PROJECT}.sep.h.Sep.g@4",
        f"{PROJECT}.sep.h.Sep.Sep",
        f"{PROJECT}.sep.h.Sep.Sep@6",
        f"{PROJECT}.sep.h.Sep.k",
        f"{PROJECT}.sep.h.Sep.k@8",
        f"{PROJECT}.box.Box.put",
        f"{PROJECT}.box.Box.put@4",
        f"{PROJECT}.fwd.h.Fwd.later",
        f"{PROJECT}.fwd.h.Fwd.later@5",
    }
    frontend_methods = get_qualified_names(get_nodes(frontend, label))
    assert frontend_methods >= expected
    assert frontend_methods == get_qualified_names(get_nodes(tree_sitter, label))


@libclang
def test_pure_libclang_frontend_binds_each_call_to_its_overload(
    temp_repo: Path,
) -> None:
    root = _pure_libclang_project(temp_repo / PROJECT)
    frontend = _MockIngestor()
    run_cpp_frontend(frontend, root, root.name, root)

    nodes = get_qualified_names(get_nodes(frontend, cs.NodeLabel.METHOD.value))
    calls = {(src, dst) for src, dst, _ in _calls(frontend)}
    sep = f"{PROJECT}.sep.h.Sep"
    assert {dst for src, dst in calls if src == f"{PROJECT}.sep.use_sep"} == {
        f"{sep}.g",
        f"{sep}.g@4",
        # `s` is a non-const reference, so the non-const `k()` is called.
        f"{sep}.k@8",
    }
    # A call into an instantiated template resolves to the member libclang
    # instantiated, which carries the template's declaration: it lands on the
    # emitted overload, never on a name no node holds.
    box_targets = {dst for src, dst in calls if src == f"{PROJECT}.box.use_box"}
    assert box_targets == {f"{PROJECT}.box.Box.put", f"{PROJECT}.box.Box.put@4"}
    assert box_targets <= nodes


@libclang
def test_pure_libclang_call_to_an_overload_declared_later_binds_to_it(
    temp_repo: Path,
) -> None:
    # The name of the overload a call targets depends on where that overload
    # is declared, not on whether the walk has reached it yet: both calls
    # pass a double, so both bind to `later(double)`, which is `later@5`.
    root = _pure_libclang_project(temp_repo / PROJECT)
    frontend = _MockIngestor()
    run_cpp_frontend(frontend, root, root.name, root)

    fwd = f"{PROJECT}.fwd.h.Fwd"
    calls = {(src, dst) for src, dst, _ in _calls(frontend)}
    assert {dst for src, dst in calls if src == f"{fwd}.a"} == {f"{fwd}.later@5"}
    assert {dst for src, dst in calls if src == f"{fwd}.b"} == {f"{fwd}.later@5"}
