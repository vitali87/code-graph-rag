"""`CppFunctionIngest`: an out-of-class method definition bound to its class
in another file, a free-function prototype held until its definition is in,
the class lookup the class pass shares, a lambda nested in an out-of-class
method's body, and the functions a module unit exports."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.cpp.function_ingest import CppFunctionIngest
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_H = "cx.src.shapes.h"
_FILES = {
    "src/shapes.h": (
        "#pragma once\n"
        "class Base {\npublic:\n  virtual void draw();\n  void paint();\n};\n"
        "Base make_base();\n"
        "int declared_only(int);\n"
    ),
    "src/shapes.cpp": (
        '#include "shapes.h"\n'
        "void Base::draw() {\n  run([]() { return 1; });\n}\n"
        "void Base::paint() {\n  auto inner = []() { return 2; };\n  inner();\n}\n"
        "Base make_base() { return Base(); }\n"
    ),
    "src/core.cppm": "export module shapes.core;\nexport int area() { return 1; }\n",
}


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "cx", mock_ingestor, _FILES)


def _qns(mock_ingestor: RecordingIngestor, label: cs.NodeLabel) -> list[str]:
    return sorted(
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, label)
        if cs.KEY_QUALIFIED_NAME in n.properties and "name" in n.properties
    )


def test_an_out_of_class_definition_is_its_classs_method(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    ingest = updater.factory.definition_processor.function_ingest.cpp
    assert isinstance(ingest, CppFunctionIngest)
    assert f"{_H}.Base.draw" in _qns(mock_ingestor, cs.NodeLabel.METHOD)
    # Not a free function of the .cpp file it is written in.
    assert "cx.src.shapes.draw" not in _qns(mock_ingestor, cs.NodeLabel.FUNCTION)


def _lambdas_defined_by(mock_ingestor: RecordingIngestor, parent: str) -> list[str]:
    return [
        target
        for source, target in edges(mock_ingestor, cs.RelationshipType.DEFINES)
        if source == parent and target.rsplit(".", 1)[-1].startswith("lambda")
    ]


def test_a_lambda_in_an_out_of_class_body_is_defined_by_that_method(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    # Its qn is the file's; its DEFINES waits for the method's final qn.
    assert _lambdas_defined_by(mock_ingestor, f"{_H}.Base.draw") == [
        "cx.src.shapes.lambda_2_6"
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CppFunctionIngest._find_enclosing_function_node stops at the first "
        "`declaration` ancestor, which the C++ spec lists among its module "
        "node types, so a lambda bound to a local (`auto inner = []{...};`) "
        "in an out-of-class method is never deferred to that method and the "
        "file's Module DEFINES it instead"
    ),
)
def test_a_lambda_bound_to_a_local_is_defined_by_its_method(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert len(_lambdas_defined_by(mock_ingestor, f"{_H}.Base.paint")) == 1


def test_a_prototype_yields_to_its_definition(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    functions = _qns(mock_ingestor, cs.NodeLabel.FUNCTION)
    # One `make_base`, the definition's; the header prototype folded into it.
    assert [qn for qn in functions if qn.endswith(".make_base")] == [
        "cx.src.shapes.make_base"
    ]
    facts = updater.factory.definition_processor.facts
    assert facts.method_return_types["cx.src.shapes.make_base"] == "Base"


def test_the_class_lookup_finds_a_class_defined_in_another_file(
    updater: GraphUpdater,
) -> None:
    ingest = updater.factory.definition_processor.function_ingest.cpp
    assert ingest.resolve_class_qn("Base", "cx.src.shapes") == (f"{_H}.Base", True)
    assert ingest.resolve_class_qn("Missing", "cx.src.shapes") == (
        "cx.src.shapes.Missing",
        False,
    )
    # The class itself is excluded when it is the one asking.
    assert ingest.resolve_class_qn("Base", "m", exclude_qn=f"{_H}.Base") == (
        "m.Base",
        False,
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "tree-sitter-cpp reads `export int area()` with `export` as the "
        "function's own type, which cpp_utils.is_exported (looking only at "
        "the siblings before the node) never sees, so the resolution is not "
        "exported and extra_relationships writes no Module EXPORTS edge for a "
        "module interface's exported function"
    ),
)
def test_an_exported_module_function_is_exported_by_its_module(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert ("cx.src.core", "cx.src.core.area") in edges(
        mock_ingestor, cs.RelationshipType.EXPORTS
    )
