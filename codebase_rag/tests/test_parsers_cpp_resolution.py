"""`CppResolver`: the destructors a `delete` runs up the class chain, the
methods a call on a template-parameter receiver may dispatch to, an operator
bound as a member or as a free function of the operand's scope, and an
operator named by its spelling."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp.resolution import CppResolver
from codebase_rag.tests.collaborator_support import index
from evals.recorded_graph import RecordingIngestor

_H = "cx.src.shapes.h"
_FILES = {
    "src/shapes.h": (
        "#pragma once\n"
        "class Base {\npublic:\n  virtual void draw();\n  virtual ~Base();\n};\n"
        "class Derived : public Base {\npublic:\n  void draw() override;\n  ~Derived();\n};\n"
        "namespace geo {\n"
        "struct Vec { int x; Vec& operator+=(const Vec& o); };\n"
        "inline Vec operator+(const Vec& a, const Vec& b) { return a; }\n"
        "}\n"
        "Base make();\n"
    ),
    "src/shapes.cpp": (
        '#include "shapes.h"\n'
        "void Base::draw() {}\nBase::~Base() {}\n"
        "void Derived::draw() {}\nDerived::~Derived() {}\n"
        "namespace geo {\n"
        "Vec& Vec::operator+=(const Vec& o) { return *this; }\n"
        "}\n"
    ),
}


@pytest.fixture
def resolver(temp_repo: Path, mock_ingestor: RecordingIngestor) -> CppResolver:
    updater = index(temp_repo / "cx", mock_ingestor, _FILES)
    cpp = updater.factory.call_processor._resolver.cpp
    assert isinstance(cpp, CppResolver)
    return cpp


def test_a_delete_runs_every_destructor_up_the_chain(resolver: CppResolver) -> None:
    assert resolver.cpp_destructor_targets(f"{_H}.Derived") == {
        (cs.NodeLabel.METHOD, f"{_H}.Derived.~Derived"),
        (cs.NodeLabel.METHOD, f"{_H}.Base.~Base"),
    }
    # A class declaring no destructor has none of its own to run.
    assert resolver.cpp_destructor_targets(f"{_H}.geo.Vec") == set()


def test_a_template_parameter_receiver_dispatches_to_every_same_named_method(
    resolver: CppResolver,
) -> None:
    targets = resolver.cpp_dispatch_targets("t.draw", {"t": "T"}, frozenset({"T"}))
    assert targets == {
        (cs.NodeLabel.METHOD, f"{_H}.Base.draw"),
        (cs.NodeLabel.METHOD, f"{_H}.Derived.draw"),
    }
    # A receiver of a concrete type is the typed lookup's to answer.
    assert (
        resolver.cpp_dispatch_targets("t.draw", {"t": "Base"}, frozenset({"T"}))
        == set()
    )
    assert resolver.cpp_dispatch_targets("draw", {}, frozenset({"T"})) == set()


def test_an_operator_binds_as_a_member_or_a_free_function_of_the_scope(
    resolver: CppResolver,
) -> None:
    vec = f"{_H}.geo.Vec"
    assert resolver.cpp_member_operator("operator_plus_assign", vec) == (
        cs.NodeLabel.METHOD,
        f"{vec}.operator_plus_assign",
    )
    assert resolver.cpp_free_operator_for_type("operator_plus", vec, 0, 2) == (
        cs.NodeLabel.FUNCTION,
        f"{_H}.geo.operator_plus",
    )
    # One operand is no binary operator's signature.
    assert resolver.cpp_free_operator_for_type("operator_plus", vec, 0, 1) is None


def test_an_operator_name_resolves_to_its_nearest_definition(
    resolver: CppResolver,
) -> None:
    found = resolver.resolve_cpp_operator_call("operator_plus", "cx.src.shapes")
    assert found is not None
    assert found[1].endswith("operator_plus")
    assert resolver.resolve_cpp_operator_call("plus", "cx.src.shapes") is None


def test_an_operand_is_typed_only_by_a_registered_class(
    resolver: CppResolver,
) -> None:
    assert resolver.cpp_operand_class_qn("v", {"v": "Derived"}, "cx.src.shapes") == (
        f"{_H}.Derived"
    )
    assert resolver.cpp_operand_class_qn("v", {"v": "int"}, "cx.src.shapes") is None
    assert (
        resolver.cpp_operand_class_qn(None, {"v": "Derived"}, "cx.src.shapes") is None
    )
