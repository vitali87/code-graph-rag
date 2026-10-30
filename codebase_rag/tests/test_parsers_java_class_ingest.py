"""`JavaClassIngest`: a method's parameter-signature qn, the `java.lang` base
a bare name stands for, the location the javac facts key a method at, and the
OVERRIDES an anonymous class's methods get to their base type."""

from __future__ import annotations

from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.parsers.java.class_ingest import (
    JavaClassIngest,
    signatured_method_qn,
)
from codebase_rag.tests.collaborator_support import edges, index, nodes_of, parse
from codebase_rag.tests.conftest import get_relationships
from evals.recorded_graph import RecordingIngestor

_SRC = "jp.src.main.java.app"
_FILES = {
    "src/main/java/app/Shape.java": (
        "package app;\npublic interface Shape { double area(); double edge(); }\n"
    ),
    "src/main/java/app/Use.java": (
        "package app;\n"
        "public class Use extends Exception {\n"
        "  Shape field = new Shape() {\n"
        "    public double area() { return 2; }\n"
        "    public double helper() { return 3; }\n"
        "  };\n"
        "}\n"
    ),
}


def _methods(source: str) -> list:
    return nodes_of(parse(cs.SupportedLanguage.JAVA, source), "method_declaration")


def test_a_methods_qn_carries_its_parameter_types() -> None:
    (with_params,) = _methods("class C { int add(int a, String b) { return a; } }")
    assert signatured_method_qn(with_params, "p.C") == "p.C.add(int,String)"
    (bare,) = _methods("class C { void run() {} }")
    assert signatured_method_qn(bare, "p.C") == "p.C.run()"


def test_a_bare_java_lang_base_is_named_in_full(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "jp", mock_ingestor, _FILES)
    ingest = updater.factory.definition_processor.class_ingest.java
    assert isinstance(ingest, JavaClassIngest)
    assert ingest.externalized_base("Exception") == "java.lang.Exception"
    assert ingest.externalized_base("Widget") is None
    (inherits,) = get_relationships(mock_ingestor, cs.RelationshipType.INHERITS)
    assert (inherits.target_label, inherits.target) == (
        cs.NodeLabel.EXTERNAL_MODULE,
        "java.lang.Exception",
    )


def test_an_anonymous_classs_method_overrides_only_its_bases_namesake(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    index(temp_repo / "jp", mock_ingestor, _FILES)
    overrides = edges(mock_ingestor, cs.RelationshipType.OVERRIDES)
    assert overrides == {(f"{_SRC}.Use.Use.area()", f"{_SRC}.Shape.Shape.area()")}


def test_a_method_is_keyed_at_its_name_token_too(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "jp", mock_ingestor, _FILES)
    locations = updater.factory.definition_processor.facts.function_locations
    # `area` in Shape.java starts at line 2, column 32.
    location = locations.get((f"{_SRC}.Shape", 2, 32))
    assert location is not None
    assert location.qualified_name == f"{_SRC}.Shape.Shape.area()"
