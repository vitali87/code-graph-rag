"""`JavaCallResolution`: the constructors an enum constant runs, the methods
and constructors a method reference hands over, the same-arity overloads a
binding cannot tell apart, and the parameter signature a Java member's qn
carries."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.java.calls import JavaCallResolution, method_member
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from codebase_rag.tests.conftest import RecordedRel, get_relationships
from evals.recorded_graph import RecordingIngestor

_SRC = "jp.src.main.java.app"
_FILES = {
    "src/main/java/app/Color.java": (
        "package app;\n"
        "public enum Color {\n"
        "  RED(1), GREEN, BLUE(2);\n"
        "  Color(int v) {}\n"
        "  Color() {}\n"
        "}\n"
    ),
    "src/main/java/app/Use.java": (
        "package app;\n"
        "import java.util.List;\n"
        "public class Use {\n"
        "  void print(String s) {}\n"
        "  void f(Object o) {}\n"
        "  void f(String s) {}\n"
        "  void run(List<String> xs, Thing t) {\n"
        "    xs.forEach(this::print);\n"
        "    java.util.function.Supplier<Box> s = Box::new;\n"
        "    f(t.value());\n"
        "  }\n"
        "}\n"
        "class Box { Box() {} }\n"
    ),
}
_RUN = f"{_SRC}.Use.Use.run(List<String>,Thing)"


@pytest.fixture
def graph(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "jp", mock_ingestor, _FILES)
    strategy = updater.factory.call_processor.calls_for(cs.SupportedLanguage.JAVA)
    assert isinstance(strategy, JavaCallResolution)
    return mock_ingestor


def _from(graph: RecordingIngestor, rel_type: str, source: str) -> list[RecordedRel]:
    return [r for r in get_relationships(graph, rel_type) if r.source == source]


def test_each_enum_constant_runs_the_constructor_its_arguments_fit(
    graph: RecordingIngestor,
) -> None:
    calls = _from(graph, cs.RelationshipType.CALLS, f"{_SRC}.Color")
    assert [
        (r.target, r.properties[cs.KEY_ARG_COUNT], r.properties["resolution"])
        for r in sorted(calls, key=lambda r: r.properties["col"])
    ] == [
        (f"{_SRC}.Color.Color.Color(int)", 1, cs.EdgeResolution.EXACT),
        (f"{_SRC}.Color.Color.Color()", 0, cs.EdgeResolution.EXACT),
        (f"{_SRC}.Color.Color.Color(int)", 1, cs.EdgeResolution.EXACT),
    ]


def test_a_method_reference_references_the_method(graph: RecordingIngestor) -> None:
    references = {r.target for r in _from(graph, cs.RelationshipType.REFERENCES, _RUN)}
    assert f"{_SRC}.Use.Use.print(String)" in references


def test_a_constructor_reference_instantiates_and_references_the_constructor(
    graph: RecordingIngestor,
) -> None:
    references = {r.target for r in _from(graph, cs.RelationshipType.REFERENCES, _RUN)}
    instantiates = {
        r.target for r in _from(graph, cs.RelationshipType.INSTANTIATES, _RUN)
    }
    assert f"{_SRC}.Use.Box.Box()" in references
    assert instantiates == {f"{_SRC}.Use.Box"}


def test_a_call_no_argument_type_decides_links_every_tied_overload(
    graph: RecordingIngestor,
) -> None:
    calls = [
        r
        for r in _from(graph, cs.RelationshipType.CALLS, _RUN)
        if r.target.startswith(f"{_SRC}.Use.Use.f(")
    ]
    assert {r.target for r in calls} == {
        f"{_SRC}.Use.Use.f(Object)",
        f"{_SRC}.Use.Use.f(String)",
    }
    assert {r.properties["resolution"] for r in calls} == {cs.EdgeResolution.OVERLOAD}


def test_a_members_name_carries_its_parameter_signature() -> None:
    root = parse(
        cs.SupportedLanguage.JAVA,
        "class C { int add(int a, String b) { return a; } void run() {} }",
    )
    add, run = nodes_of(root, "method_declaration")
    assert method_member(add, "add") == "add(int,String)"
    assert method_member(run, "run") == "run()"
