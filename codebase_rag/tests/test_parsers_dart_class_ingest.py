"""`DartClassIngest`: the type arguments an `extends` clause passes, the type
an extension is on, a class's field types and its methods' return types, the
constructors among them, and the `@override` methods that override only
external code."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.dart.class_ingest import DartClassIngest
from codebase_rag.tests.collaborator_support import index
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_M = "dp.lib.shapes"
_SHAPES = (
    "abstract class Shape {\n  double area();\n}\n\n"
    "class Point {\n"
    "  final int x;\n"
    "  Point(this.x);\n"
    "  Point.origin() : this(0);\n"
    "  Point move(int d) => Point(x + d);\n"
    "}\n\n"
    "class Square extends Shape {\n"
    "  @override\n  double area() => 1;\n"
    "  @override\n  String toString() => 'sq';\n"
    "}\n\n"
    "class Box<T> {\n  T? item;\n}\n\n"
    "class PointBox extends Box<Point> {}\n\n"
    "extension PointMath on Point {\n  int sum() => x;\n}\n"
)


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "dp", mock_ingestor, {"lib/shapes.dart": _SHAPES})


def test_the_heritage_an_extends_clause_and_an_extension_write(
    updater: GraphUpdater,
) -> None:
    assert isinstance(
        updater.factory.definition_processor.class_ingest.dart, DartClassIngest
    )
    facts = updater.factory.definition_processor.facts
    assert facts.dart_extends_type_args[f"{_M}.PointBox"] == [f"{_M}.Point"]
    assert facts.dart_extension_on_types[f"{_M}.PointMath"] == "Point"


def test_field_and_return_types_are_recorded_and_constructors_known(
    updater: GraphUpdater,
) -> None:
    facts = updater.factory.definition_processor.facts
    assert facts.class_field_types[f"{_M}.Point"] == {"x": "int"}
    assert facts.method_return_types[f"{_M}.Point.move"] == "Point"
    assert {f"{_M}.Point.Point", f"{_M}.Point.origin"} <= facts.dart_constructor_qns
    assert f"{_M}.Point.move" not in facts.dart_constructor_qns


def test_only_an_override_no_project_ancestor_defines_is_marked_external(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    external = {
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.METHOD)
        if n.properties.get(cs.KEY_OVERRIDES_EXTERNAL) is True
    }
    # `toString` overrides Object's; `area` overrides the project's Shape.
    assert external == {f"{_M}.Square.toString"}
