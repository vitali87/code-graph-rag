"""`CppCallResolution`: a virtual call's dispatch to the overrides, the
destructors a `delete` and a destructor's own end run, an operator bound to
its overload, a construction, a call through a pointer alias, and a function
pointer handed over and called."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.cpp.calls import CCallResolution, CppCallResolution
from codebase_rag.tests.collaborator_support import edges, index
from evals.recorded_graph import RecordingIngestor

_H = "cx.src.shapes.h"
_RUN = "cx.src.shapes.run"
_FILES = {
    "src/shapes.h": (
        "#pragma once\n"
        "class Base {\npublic:\n  virtual void draw();\n  virtual ~Base();\n};\n"
        "class Derived : public Base {\npublic:\n  void draw() override;\n  ~Derived();\n};\n"
        "struct Vec { int x; };\n"
        "Vec operator+(const Vec& a, const Vec& b);\n"
        "using DerivedPtr = Derived*;\n"
    ),
    "src/shapes.cpp": (
        '#include "shapes.h"\n'
        "void Base::draw() {}\nBase::~Base() {}\n"
        "void Derived::draw() {}\nDerived::~Derived() {}\n"
        "Vec operator+(const Vec& a, const Vec& b) { return a; }\n"
        "void helper() {}\n"
        "void apply(void (*f)()) { f(); }\n"
        "void run() {\n"
        "  Base* b = new Derived();\n"
        "  b->draw();\n"
        "  delete b;\n"
        "  Vec a, c;\n"
        "  Vec s = a + c;\n"
        "  apply(helper);\n"
        "  DerivedPtr d = nullptr;\n"
        "  d->draw();\n"
        "}\n"
    ),
    "src/plain.c": "static int twice(int x) { return 2 * x; }\nint main(void) { return twice(1); }\n",
}


@pytest.fixture
def graph(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "cx", mock_ingestor, _FILES)
    processor = updater.factory.call_processor
    assert isinstance(processor.calls_for(cs.SupportedLanguage.CPP), CppCallResolution)
    assert isinstance(processor.calls_for(cs.SupportedLanguage.C), CCallResolution)
    return mock_ingestor


def _calls_from(graph: RecordingIngestor, source: str) -> set[str]:
    return {t for s, t in edges(graph, cs.RelationshipType.CALLS) if s == source}


def test_a_virtual_call_reaches_the_overrides(graph: RecordingIngestor) -> None:
    calls = _calls_from(graph, _RUN)
    assert {f"{_H}.Base.draw", f"{_H}.Derived.draw"} <= calls


def test_a_delete_and_a_destructor_run_the_destructor_chain(
    graph: RecordingIngestor,
) -> None:
    calls = _calls_from(graph, _RUN)
    assert {f"{_H}.Base.~Base", f"{_H}.Derived.~Derived"} <= calls
    # A destructor runs its bases' when it ends.
    assert _calls_from(graph, f"{_H}.Derived.~Derived") == {f"{_H}.Base.~Base"}


def test_an_operator_and_a_construction_bind_their_definitions(
    graph: RecordingIngestor,
) -> None:
    assert "cx.src.shapes.operator_plus" in _calls_from(graph, _RUN)
    assert (_RUN, f"{_H}.Derived") in edges(graph, cs.RelationshipType.INSTANTIATES)


def test_a_function_pointer_handed_over_is_referenced_and_called(
    graph: RecordingIngestor,
) -> None:
    assert (_RUN, "cx.src.shapes.helper") in edges(
        graph, cs.RelationshipType.REFERENCES
    )
    assert "cx.src.shapes.apply" in _calls_from(graph, _RUN)
    assert _calls_from(graph, "cx.src.shapes.apply") == {"cx.src.shapes.helper"}


def test_a_c_file_calls_through_the_c_strategy(graph: RecordingIngestor) -> None:
    assert _calls_from(graph, "cx.src.plain.main") == {"cx.src.plain.twice"}
