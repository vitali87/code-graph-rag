"""`DartCallResolution`: the getter reads and constructor runs Dart spells
without a call node, the extension member a receiver's chain reaches, and the
class a named constructor call constructs."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.dart.calls import DartCallResolution
from codebase_rag.parsers.language_calls import CallName
from codebase_rag.tests.collaborator_support import edges, first, index, parse
from evals.recorded_graph import RecordingIngestor

_POINT = "dp.lib.point.Point"
_MAIN = "dp.lib.main.main"
_FILES = {
    "pubspec.yaml": "name: dp\n",
    "lib/point.dart": (
        "class Point {\n"
        "  final int x;\n"
        "  Point(this.x);\n"
        "  Point.origin() : this(0);\n"
        "  Point move(int d) => Point(x + d);\n"
        "  int get doubled => x * 2;\n"
        "}\n\n"
        "extension PointMath on Point {\n  int sum() => x;\n}\n"
    ),
    "lib/main.dart": (
        "import 'point.dart' as p;\n\n"
        "void main() {\n"
        "  final q = p.Point(1).move(2).sum();\n"
        "  final o = p.Point.origin();\n"
        "  print(o.doubled);\n"
        "}\n"
    ),
}


@pytest.fixture
def graph(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "dp", mock_ingestor, _FILES)
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.DART)
    assert isinstance(strategy, DartCallResolution)
    return mock_ingestor


def test_a_named_constructor_call_constructs_its_class(
    graph: RecordingIngestor,
) -> None:
    assert (_MAIN, _POINT) in edges(graph, cs.RelationshipType.INSTANTIATES)
    assert (_MAIN, f"{_POINT}.origin") in edges(graph, cs.RelationshipType.CALLS)


def test_a_redirecting_constructor_runs_the_one_it_delegates_to(
    graph: RecordingIngestor,
) -> None:
    assert (f"{_POINT}.origin", f"{_POINT}.Point") in edges(
        graph, cs.RelationshipType.CALLS
    )


def test_a_chain_reaches_an_extension_member_after_the_classs_own(
    graph: RecordingIngestor,
) -> None:
    calls = edges(graph, cs.RelationshipType.CALLS)
    assert (_MAIN, f"{_POINT}.move") in calls
    assert (_MAIN, "dp.lib.point.PointMath.sum") in calls


def test_a_getter_read_references_the_getter(graph: RecordingIngestor) -> None:
    assert (_MAIN, f"{_POINT}.doubled") in edges(graph, cs.RelationshipType.REFERENCES)
    # A read runs the getter but is no call node.
    assert (_MAIN, f"{_POINT}.doubled") not in edges(graph, cs.RelationshipType.CALLS)


def test_calls_and_methods_are_named_by_the_dart_shapes(
    temp_repo: Path, mock_ingestor: RecordingIngestor
) -> None:
    updater = index(temp_repo / "dp", mock_ingestor, {"lib/a.dart": "void f() {}\n"})
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.DART)
    root = parse(cs.SupportedLanguage.DART, "class C {\n  C.named();\n}\n")
    # A named constructor's `name` field is its CLASS; the declared name is
    # the last identifier.
    ctor = first(root, "constructor_signature")
    assert strategy.method_name(ctor, "dp.lib.a") == "named"
    call_root = parse(cs.SupportedLanguage.DART, "void f() { helper(1); }\n")
    assert strategy.call_name(first(call_root, "selector")) == CallName(True, "helper")
