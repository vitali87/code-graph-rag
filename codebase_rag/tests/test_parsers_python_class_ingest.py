"""`PythonClassIngest`: the plain name a class reserves ahead of a same-named
function, the member names its body binds a method to and the names it binds
to no method, a checker-only class, an enum, and the method names an
external stdlib base defines."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.python.class_ingest import PythonClassIngest
from codebase_rag.tests.collaborator_support import first, index, parse
from codebase_rag.tests.conftest import get_nodes
from evals.recorded_graph import RecordingIngestor

_MOD = (
    "import enum\nimport json\nfrom typing import TYPE_CHECKING\n\n"
    "class Thing:\n    pass\n\n"
    "def Thing():\n    return 1\n\n"
    "class Color(enum.Enum):\n    RED = 1\n\n"
    "class Encoder(json.JSONEncoder):\n    def default(self, o):\n        return o\n\n"
    "if TYPE_CHECKING:\n    class OnlyForChecks:\n        pass\n"
)


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "proj", mock_ingestor, {"mod.py": _MOD})


def _ingest(updater: GraphUpdater) -> PythonClassIngest:
    ingest = updater.factory.definition_processor.class_ingest.python
    assert isinstance(ingest, PythonClassIngest)
    return ingest


def _classes(mock_ingestor: RecordingIngestor) -> dict[str, dict]:
    return {
        str(n.properties[cs.KEY_QUALIFIED_NAME]): dict(n.properties)
        for n in get_nodes(mock_ingestor, cs.NodeLabel.CLASS)
    }


def test_a_class_keeps_its_plain_name_over_a_later_same_named_function(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    assert "proj.mod.Thing" in _classes(mock_ingestor)
    functions = {
        str(n.properties[cs.KEY_QUALIFIED_NAME])
        for n in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }
    assert "proj.mod.Thing" not in functions
    assert any(qn.startswith("proj.mod.Thing@") for qn in functions), functions


def test_a_class_under_type_checking_is_marked_checker_only(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    classes = _classes(mock_ingestor)
    assert classes["proj.mod.OnlyForChecks"].get(cs.KEY_TYPE_CHECKING_ONLY) is True
    assert cs.KEY_TYPE_CHECKING_ONLY not in classes["proj.mod.Thing"]


def test_an_enum_is_known_by_its_resolved_bases(updater: GraphUpdater) -> None:
    ingest = _ingest(updater)
    assert ingest.is_enum("proj.mod.Color")
    assert not ingest.is_enum("proj.mod.Thing")


def test_an_external_stdlib_base_lends_its_method_names(updater: GraphUpdater) -> None:
    ingest = _ingest(updater)
    assert "default" in ingest.external_override_names("proj.mod.Encoder")
    assert ingest.external_override_names("proj.mod.Thing") == frozenset()


def test_member_aliases_and_value_members_are_read_off_the_body() -> None:
    root = parse(
        cs.SupportedLanguage.PYTHON,
        "class C:\n    def _impl(self): ...\n    run = _impl\n    size = 0\n",
    )
    class_node = first(root, cs.TS_PY_CLASS_DEFINITION)
    body = class_node.child_by_field_name(cs.FIELD_BODY)
    assert body is not None
    impl = first(class_node, cs.TS_PY_FUNCTION_DEFINITION)
    assert PythonClassIngest.member_aliases(body) == {impl.id: ("run",)}
    assert PythonClassIngest.non_method_members(class_node) == ["size"]
