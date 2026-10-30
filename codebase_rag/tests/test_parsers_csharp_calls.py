"""`CSharpCallResolution`: a target-typed `new()`, a conditional access, a
method group, a property read, the generic arity a construction picks, and
the names those shapes carry."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.csharp.calls import (
    CSharpCallResolution,
    conditional_access_call_name,
    member_access_call_name,
    target_typed_new_name,
)
from codebase_rag.tests.collaborator_support import (
    edges,
    first,
    index,
    nodes_of,
    parse,
)
from evals.recorded_graph import RecordingIngestor

_MODELS = "cp.Widget.App.Models"
_GO = "cp.Use.App.Services.User.Go(List)"
_FILES = {
    "App.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>\n',
    "Widget.cs": (
        "namespace App.Models {\n"
        "  public class Widget {\n"
        "    public int Size { get { return 1; } }\n"
        "    public void Run() {}\n"
        "    public static Widget Make() { return new(); }\n"
        "  }\n"
        "  public class Box {}\n"
        "  public class Box<T> {}\n"
        "}\n"
    ),
    "Use.cs": (
        "using System.Collections.Generic;\nusing App.Models;\n\n"
        "namespace App.Services {\n"
        "  public class User {\n"
        "    void Print(int x) {}\n"
        "    void Go(List<int> xs) {\n"
        "      Widget w = new();\n"
        "      w?.Run();\n"
        "      var n = w.Size;\n"
        "      xs.ForEach(Print);\n"
        "      var b = new Box<int>();\n"
        "      var c = new Box();\n"
        "      App.Models.Widget.Make();\n"
        "    }\n"
        "  }\n"
        "}\n"
    ),
}


@pytest.fixture
def graph(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RecordingIngestor:
    updater = index(temp_repo / "cp", mock_ingestor, _FILES)
    strategy = updater.factory.call_processor._calls_for(cs.SupportedLanguage.CSHARP)
    assert isinstance(strategy, CSharpCallResolution)
    return mock_ingestor


def test_a_target_typed_new_constructs_the_declared_type(
    graph: RecordingIngestor,
) -> None:
    instantiates = edges(graph, cs.RelationshipType.INSTANTIATES)
    assert (_GO, f"{_MODELS}.Widget") in instantiates
    # In a return, the method's return type is the target.
    assert (f"{_MODELS}.Widget.Make", f"{_MODELS}.Widget") in instantiates


def test_a_construction_picks_the_type_of_its_written_arity(
    graph: RecordingIngestor,
) -> None:
    instantiates = edges(graph, cs.RelationshipType.INSTANTIATES)
    assert (_GO, f"{_MODELS}.Box`1") in instantiates
    assert (_GO, f"{_MODELS}.Box") in instantiates


def test_conditional_access_and_qualified_calls_bind_their_methods(
    graph: RecordingIngestor,
) -> None:
    calls = edges(graph, cs.RelationshipType.CALLS)
    assert (_GO, f"{_MODELS}.Widget.Run") in calls
    assert (_GO, f"{_MODELS}.Widget.Make") in calls


def test_a_method_group_and_a_property_read_reference_their_members(
    graph: RecordingIngestor,
) -> None:
    references = edges(graph, cs.RelationshipType.REFERENCES)
    assert (_GO, "cp.Use.App.Services.User.Print(int)") in references
    assert (_GO, f"{_MODELS}.Widget.Size") in references


def test_the_call_shapes_name_receiver_and_member() -> None:
    root = parse(
        cs.SupportedLanguage.CSHARP,
        "class C { Widget M() { a.Run<int>(); b?.Stop(); Widget w = new(); "
        "var v = new(); return new(); } }",
    )
    assert member_access_call_name(first(root, "member_access_expression")) == "a.Run"
    assert (
        conditional_access_call_name(first(root, "conditional_access_expression"))
        == "b.Stop"
    )
    # Typed by the declaration or the method's return; `var` names nothing.
    assert [
        target_typed_new_name(node)
        for node in nodes_of(root, "implicit_object_creation_expression")
    ] == ["Widget", None, "Widget"]
