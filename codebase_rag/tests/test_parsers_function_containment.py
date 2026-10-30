"""`FunctionContainment`: the parent a function's DEFINES edge comes from,
and the edges held until every parent has registered. A module parent is
written at once; any other waits, then binds to the parent that did
register: the node at its recorded span, its own qn, a lexical fallback, the
named claim on an anonymous placeholder, and else its module."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import LANGUAGE_SPECS
from codebase_rag.parsers.definition_context import DefinitionContext
from codebase_rag.parsers.function_containment import FunctionContainment
from codebase_rag.tests.collaborator_support import index, nodes_of, parse
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import DeferredParentLink
from evals.recorded_graph import RecordingIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_PY = cs.SupportedLanguage.PYTHON
_JS = cs.SupportedLanguage.JS
_PY_SOURCE = """\
def outer():
    def inner():
        pass
    return inner

class Box:
    def method(self):
        def local():
            pass
        return local

if True:
    def flagged():
        pass
"""
_JS_SOURCE = (
    "exports.receiver = function () {\n  function helper() {}\n  return helper;\n};\n"
    "function top() {\n  const arrow = () => {\n    function deep() {}\n  };\n}\n"
)
_FUNCTION = cs.NodeLabel.FUNCTION


@dataclass
class _Containment:
    containment: FunctionContainment
    links: list[DeferredParentLink]
    ingestor: RecordingIngestor

    def defines(self) -> set[tuple[str, str, str, str]]:
        return {
            (rel.source_label, str(rel.source), rel.rel_type, str(rel.target))
            for rel in get_relationships(self.ingestor)
        }


def _containment_over(
    updater: GraphUpdater, links: list[DeferredParentLink]
) -> FunctionContainment:
    # Built as the function pass builds its own, over the index's registry
    # and facts, with a queue this test holds.
    ingest = updater.factory.definition_processor.function_ingest
    context = DefinitionContext(
        ingestor=ingest.ingestor,
        repo_path=ingest.repo_path,
        project_name=updater.project_name,
        function_registry=ingest.function_registry,
        simple_name_lookup=ingest.simple_name_lookup,
        module_qn_to_file_path=ingest.module_qn_to_file_path,
        import_processor=updater.factory.import_processor,
        facts=ingest.facts,
    )
    return FunctionContainment(ingest, context, links)


@pytest.fixture
def held(temp_repo: Path, mock_ingestor: RecordingIngestor) -> _Containment:
    updater = index(
        temp_repo / "proj", mock_ingestor, {"m.py": _PY_SOURCE, "r.js": _JS_SOURCE}
    )
    # Only what the containment under test writes is under test.
    mock_ingestor.reset_mock()
    links: list[DeferredParentLink] = []
    return _Containment(_containment_over(updater, links), links, mock_ingestor)


def _function(language: cs.SupportedLanguage, source: str, name: str) -> Node:
    node_type = (
        cs.TS_PY_FUNCTION_DEFINITION if language == _PY else "function_declaration"
    )
    return next(
        node
        for node in nodes_of(parse(language, source), node_type)
        if (name_node := node.child_by_field_name(cs.FIELD_NAME)) is not None
        and name_node.text == name.encode()
    )


def test_a_module_parent_is_written_at_once(held: _Containment) -> None:
    held.containment.emit_or_defer_defines(
        cs.NodeLabel.MODULE, "proj.m", _FUNCTION, "proj.m.outer", "proj.m"
    )
    assert held.defines() == {("Module", "proj.m", "DEFINES", "proj.m.outer")}
    assert held.links == []
    assert held.containment.resolve_deferred_parent_links() == 0


def test_any_other_parent_waits_then_binds_under_its_registered_label(
    held: _Containment,
) -> None:
    # Guessed a Function; the class pass registered it as a Method.
    held.containment.emit_or_defer_defines(
        _FUNCTION, "proj.m.Box.method", _FUNCTION, "proj.m.Box.method.local", "proj.m"
    )
    assert held.defines() == set()
    assert len(held.links) == 1

    assert held.containment.resolve_deferred_parent_links() == 1
    assert held.defines() == {
        ("Method", "proj.m.Box.method", "DEFINES", "proj.m.Box.method.local")
    }
    # Emptied in place, so the class pass sharing the queue sees it empty.
    assert held.links == []
    assert held.containment.resolve_deferred_parent_links() == 0


def test_a_phantom_parent_binds_to_the_registered_lexical_fallback(
    held: _Containment,
) -> None:
    held.containment.emit_or_defer_defines(
        _FUNCTION,
        "proj.m.ghost",
        _FUNCTION,
        "proj.m.outer.inner",
        "proj.m",
        fallback_label=_FUNCTION,
        fallback_qn="proj.m.outer",
    )
    held.containment.resolve_deferred_parent_links()
    assert held.defines() == {
        ("Function", "proj.m.outer", "DEFINES", "proj.m.outer.inner")
    }


def test_an_anonymous_placeholder_binds_to_the_named_claim_on_its_span(
    held: _Containment,
) -> None:
    # `anonymous_0_19` stood for the function expression at row 0, col 19,
    # which `exports.receiver = function () {...}` claimed by name.
    held.containment.emit_or_defer_defines(
        _FUNCTION, "proj.r.anonymous_0_19", _FUNCTION, "proj.r.helper", "proj.r"
    )
    # A placeholder whose position does not parse claims nothing.
    held.containment.emit_or_defer_defines(
        _FUNCTION, "proj.r.anonymous_0_x", _FUNCTION, "proj.r.top", "proj.r"
    )
    held.containment.resolve_deferred_parent_links()
    assert held.defines() == {
        ("Function", "proj.r.receiver", "DEFINES", "proj.r.helper"),
        ("Module", "proj.r", "DEFINES", "proj.r.top"),
    }


def test_a_recorded_span_beats_the_guessed_qn(held: _Containment) -> None:
    held.containment.emit_or_defer_defines(
        _FUNCTION,
        "proj.m.Box.method_guess",
        _FUNCTION,
        "proj.m.Box.method.local",
        "proj.m",
        parent_span=("proj.m", 7, 4),
    )
    # A span nothing was recorded at leaves the guess to decide.
    held.containment.emit_or_defer_defines(
        _FUNCTION,
        "proj.m.outer",
        _FUNCTION,
        "proj.m.outer.inner",
        "proj.m",
        parent_span=("proj.m", 99, 0),
    )
    held.containment.resolve_deferred_parent_links()
    assert held.defines() == {
        ("Method", "proj.m.Box.method", "DEFINES", "proj.m.Box.method.local"),
        ("Function", "proj.m.outer", "DEFINES", "proj.m.outer.inner"),
    }


def test_an_unregistered_container_anchors_its_child_to_the_module(
    held: _Containment,
) -> None:
    # The class pass queues its methods here with their own relationship.
    held.links.append(
        DeferredParentLink(
            cs.NodeLabel.CLASS,
            "proj.m.Box",
            cs.NodeLabel.METHOD,
            "proj.m.Box.method",
            "proj.m",
            cs.RelationshipType.DEFINES_METHOD.value,
        )
    )
    held.links.append(
        DeferredParentLink(
            cs.NodeLabel.CLASS,
            "proj.m.Gone",
            cs.NodeLabel.METHOD,
            "proj.m.Gone.method",
            "proj.m",
            cs.RelationshipType.DEFINES_METHOD.value,
        )
    )
    assert held.containment.resolve_deferred_parent_links() == 2
    # DEFINES_METHOD from a Module is no documented shape, so the orphan
    # takes plain DEFINES.
    assert held.defines() == {
        ("Class", "proj.m.Box", "DEFINES_METHOD", "proj.m.Box.method"),
        ("Module", "proj.m", "DEFINES", "proj.m.Gone.method"),
    }


def test_a_nested_function_binds_to_the_function_or_method_around_it(
    held: _Containment,
) -> None:
    spec = LANGUAGE_SPECS[_PY]

    def parent(name: str, qn: str) -> tuple[str, str, object]:
        return held.containment.determine_function_parent(
            _function(_PY, _PY_SOURCE, name), qn, "proj.m", spec, _PY
        )

    assert parent("inner", "proj.m.outer.inner") == (_FUNCTION, "proj.m.outer", None)
    assert parent("local", "proj.m.Box.method.local") == (
        cs.NodeLabel.METHOD,
        "proj.m.Box.method",
        None,
    )
    assert parent("outer", "proj.m.outer") == (cs.NodeLabel.MODULE, "proj.m", None)
    # An `if` block is no scope: a function in one is the module's.
    assert parent("flagged", "proj.m.flagged") == (cs.NodeLabel.MODULE, "proj.m", None)


def test_a_function_in_an_anonymous_arrow_binds_to_the_arrow(
    held: _Containment,
) -> None:
    deep = _function(_JS, _JS_SOURCE, "deep")
    # The arrow adds no segment to `deep`'s qn; trimming that qn would hoist
    # `deep` past the arrow to `top`.
    assert held.containment.determine_function_parent(
        deep, "proj.r.top.deep", "proj.r", LANGUAGE_SPECS[_JS], _JS
    ) == (_FUNCTION, "proj.r.top.arrow", None)
