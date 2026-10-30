"""`CppClassIngest`: the class captures that register nothing or wait (a
forward declaration until its definition is known, a template's inner
specifier, an elaborated type use), the bases resolved once every header is
parsed, a C++20 module's units, and what a type's body records."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.cpp.class_ingest import CClassIngest, CppClassIngest
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_FILES = {
    "src/fwd.h": (
        "class Opaque;\n"
        "class Fwd;\n"
        "class Fwd { public: int get(); };\n"
        "struct S { int x; double y; };\n"
        "struct S s_var;\n"
        "struct Holder { Fwd f; Opaque* o; int n; };\n"
    ),
    "src/base.h": "class Base { public: virtual ~Base(); };\n",
    "src/derived.h": (
        '#include "base.h"\n'
        "class Derived : public Base { public: Derived* self() { return this; } };\n"
        "template <typename T> class Box { T item; };\n"
    ),
    "src/core.cppm": "export module shapes.core;\nexport int area() { return 1; }\n",
    "src/core_impl.cpp": "module shapes.core;\nint helper() { return 2; }\n",
    "src/ghost_impl.cpp": "module shapes.ghost;\nint g() { return 1; }\n",
}


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "cx", mock_ingestor, _FILES)


def _classes(mock_ingestor: RecordingIngestor) -> list[str]:
    return sorted(
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
    )


def test_a_forward_declaration_registers_only_when_nothing_defines_it(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = updater.factory.definition_processor.class_ingest
    assert isinstance(ingest.cpp, CppClassIngest)
    assert isinstance(ingest.c, CClassIngest)
    classes = _classes(mock_ingestor)
    # One `Fwd`, the definition; `Opaque` is declared only, so it stands.
    assert classes.count("cx.src.fwd.Fwd") == 1
    assert "cx.src.fwd.Opaque" in classes
    # `struct S s_var;` uses the type; it declares nothing.
    assert classes.count("cx.src.fwd.S") == 1


def test_a_base_in_another_header_resolves_once_every_class_is_in(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert edges(mock_ingestor, cs.RelationshipType.INHERITS) == {
        ("cx.src.derived.Derived", "cx.src.base.Base")
    }
    facts = updater.factory.definition_processor.facts
    assert facts.class_inheritance["cx.src.derived.Derived"] == ["cx.src.base.Base"]


def test_a_types_fields_and_method_return_types_are_recorded(
    updater: GraphUpdater,
) -> None:
    facts = updater.factory.definition_processor.facts
    # A field of a class type is recorded; a builtin types no member call.
    assert facts.class_field_types["cx.src.fwd.Holder"] == {"f": "Fwd", "o": "Opaque"}
    assert facts.method_return_types["cx.src.derived.Derived.self"] == "Derived"


def test_a_module_implementation_implements_only_a_parsed_interface(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert updater.factory.definition_processor.class_ingest.cpp.module_interfaces == {
        "cx.shapes.core"
    }
    # `shapes.ghost` has no interface unit: its edge would be a phantom.
    assert edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS) == {
        ("cx.shapes.core_impl", "cx.shapes.core")
    }


def test_an_exported_module_class_is_registered_under_its_own_name(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    # The grammar reads none of these as a class specifier: `export` parses
    # as a type, the keyword as a declarator, and an empty body as a
    # braced initialiser.
    index(
        temp_repo / "cx",
        mock_ingestor,
        {
            "a.cppm": (
                "export module a;\n"
                "export class Shape { public: int area(); };\n"
                "export struct Point { int x; };\n"
                "export class Empty {};\n"
                "export class Fwd;\n"
            )
        },
    )
    assert _classes(mock_ingestor) == ["cx.a.Empty", "cx.a.Point", "cx.a.Shape"]
    assert {
        target
        for source, target in edges(mock_ingestor, cs.RelationshipType.EXPORTS)
        if source == "cx.a"
    } == {"cx.a.Empty", "cx.a.Point", "cx.a.Shape"}
    assert edges(mock_ingestor, cs.RelationshipType.DEFINES_METHOD) == {
        ("cx.a.Shape", "cx.a.Shape.area")
    }
