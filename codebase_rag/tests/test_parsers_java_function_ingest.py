"""`JavaFunctionIngest` records the override a method-body anonymous class
declares, which the function pass captures as a function and the class pass
links to its base type once every type is registered."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parsers.java.function_ingest import (
    JavaFunctionIngest,
    _anonymous_base_type,
)
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from evals.recorded_graph import RecordingIngestor

_SRC = "jp.src.main.java.app"
_CLASS_TYPES = frozenset(LANGUAGE_SPECS[cs.SupportedLanguage.JAVA].class_node_types)
_USE = (
    "package app;\n"
    "public class Use {\n"
    "  Shape make() {\n"
    "    return new Shape() { public double area() { return 1; } };\n"
    "  }\n"
    "  void sort() {\n"
    "    new java.util.Comparator<String>() {\n"
    "      public int compare(String a, String b) { return 0; }\n"
    "    };\n"
    "  }\n"
    "}\n"
)


def test_a_method_body_anonymous_override_overrides_its_project_base(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(
        temp_repo / "jp",
        mock_ingestor,
        {
            "src/main/java/app/Shape.java": (
                "package app;\npublic interface Shape { double area(); }\n"
            ),
            "src/main/java/app/Use.java": _USE,
        },
    )
    ingest = updater.factory.definition_processor.function_ingest.java
    assert isinstance(ingest, JavaFunctionIngest)
    # Recorded for both; only the project base can be linked.
    assert {base for _qn, _name, base, _module in ingest.anon_overrides} >= {
        "Shape",
        "java.util.Comparator",
    }
    assert edges(mock_ingestor, cs.RelationshipType.OVERRIDES) == {
        (f"{_SRC}.Use.Use.make.area", f"{_SRC}.Shape.Shape.area()")
    }


def test_the_base_type_is_the_created_type_without_type_arguments() -> None:
    methods = nodes_of(parse(cs.SupportedLanguage.JAVA, _USE), "method_declaration")
    by_name = {m.child_by_field_name("name").text.decode(): m for m in methods}
    assert _anonymous_base_type(by_name["area"], _CLASS_TYPES) == "Shape"
    assert (
        _anonymous_base_type(by_name["compare"], _CLASS_TYPES) == "java.util.Comparator"
    )


def test_a_method_of_a_declared_class_is_no_anonymous_override() -> None:
    methods = nodes_of(parse(cs.SupportedLanguage.JAVA, _USE), "method_declaration")
    make = next(m for m in methods if m.child_by_field_name("name").text == b"make")
    assert _anonymous_base_type(make, _CLASS_TYPES) is None
