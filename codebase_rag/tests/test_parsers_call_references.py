"""`ReferenceWalkers`: the edges a scope makes to the functions it holds as
values, with no call node to carry them. An assignment, a `return`, a
parameter default and a JSX element each reference the function they name;
each walk stops at the nested scopes that get a pass of their own, and the
module's walk covers what lies outside every function."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.call_references import ReferenceWalkers
from codebase_rag.parsers.scope_boundaries import flow_scope_boundaries
from codebase_rag.tests.collaborator_support import first, index, nodes_of, parse
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import LanguageQueries
from evals.recorded_graph import RecordingIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_PY = cs.SupportedLanguage.PYTHON
_TSX = cs.SupportedLanguage.TSX
_PY_SOURCE = """\
def helper():
    pass

def left():
    pass

def right():
    pass

def is_ready():
    pass

def nested_only():
    pass

def fallback():
    pass

HANDLERS = {"a": helper}
alias = helper

class Router:
    routes = {"x": left}
    cb = right
    def route(self):
        local = nested_only

def runner(conv=fallback, n=1):
    cb = helper
    count = 1
    pick = left if is_ready else right
    def inner():
        x = nested_only
        return nested_only
    return helper, (left,)
"""
_TSX_SOURCE = """\
interface Shape { x: number }
function Card() { return null; }
function handleClick() {}
function fallback() {}
function isSame() {}
function noop() {}
function identity() {}
export function App(props) {
  const onSave = props.onSave || fallback;
  const same = props.f === isSame;
  return (
    <div onClick={handleClick}>
      <Card />
      <Shape />
      <span title={props.name || noop} />
    </div>
  );
}
const make = () => noop;
const ident = (noop) => noop;
function use(api, selector = identity) { return api; }
"""


@dataclass
class _Indexed:
    processor: CallProcessor
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    ingestor: RecordingIngestor

    @property
    def walkers(self) -> ReferenceWalkers:
        return self.processor.references

    def boundaries(self, language: cs.SupportedLanguage) -> frozenset[str]:
        return flow_scope_boundaries(self.queries[language][cs.QUERY_CONFIG])

    def written(self, rel_type: str = cs.RelationshipType.REFERENCES) -> set[str]:
        # The targets of what the walk under test wrote, from any source.
        return {str(rel.target) for rel in get_relationships(self.ingestor, rel_type)}

    def sources(self) -> set[str]:
        return {str(rel.source) for rel in get_relationships(self.ingestor)}


@pytest.fixture
def indexed(temp_repo: Path, mock_ingestor: RecordingIngestor) -> _Indexed:
    updater = index(
        temp_repo / "proj",
        mock_ingestor,
        {"m.py": _PY_SOURCE, "app.tsx": _TSX_SOURCE},
    )
    # The walks under test write through the same ingestor as the index did;
    # only what they write is under test.
    mock_ingestor.reset_mock()
    return _Indexed(updater.factory.call_processor, updater.queries, mock_ingestor)


def _named(root: Node, node_type: str, name: str) -> Node:
    return next(
        node
        for node in nodes_of(root, node_type)
        if (name_node := node.child_by_field_name(cs.FIELD_NAME)) is not None
        and name_node.text == name.encode()
    )


def _spec(qn: str) -> tuple[str, str, str]:
    return (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, qn)


def _runner() -> Node:
    return _named(parse(_PY, _PY_SOURCE), cs.TS_PY_FUNCTION_DEFINITION, "runner")


def test_an_assignment_references_the_function_it_binds(indexed: _Indexed) -> None:
    site = first(parse(_PY, "x\n"), cs.TS_PY_IDENTIFIER)
    indexed.processor.site_node = site
    indexed.walkers.ingest_assignment_function_references(
        _runner(),
        _spec("proj.m.runner"),
        "proj.m",
        None,
        None,
        indexed.boundaries(_PY),
        "proj.m.runner",
        language=_PY,
    )
    # Both results of the ternary, never its truthiness-tested condition;
    # `count = 1` binds no function, and the nested def's assignment is its
    # own pass's.
    assert indexed.written() == {"proj.m.helper", "proj.m.left", "proj.m.right"}
    assert indexed.sources() == {"proj.m.runner"}
    # The walk moved the edge site to each node it visited and put it back.
    assert indexed.processor.site_node is site


def test_a_return_references_the_functions_it_hands_back(indexed: _Indexed) -> None:
    indexed.walkers.ingest_returned_function_references(
        _runner(),
        _spec("proj.m.runner"),
        "proj.m",
        None,
        None,
        indexed.boundaries(_PY),
        "proj.m.runner",
        _PY,
    )
    # A returned tuple hides its functions one level down; the nested def's
    # `return nested_only` belongs to that def.
    assert indexed.written() == {"proj.m.helper", "proj.m.left"}


def test_a_parameter_default_references_the_function_it_names(
    indexed: _Indexed,
) -> None:
    indexed.walkers.ingest_default_param_references(
        _runner(), _spec("proj.m.runner"), "proj.m", None, None, "proj.m.runner"
    )
    assert indexed.written() == {"proj.m.fallback"}

    # TS keeps a default in the parameter's `value` field, not `right`.
    use = _named(parse(_TSX, _TSX_SOURCE), "function_declaration", "use")
    indexed.walkers.ingest_default_param_references(
        use, _spec("proj.app.use"), "proj.app", None, None, "proj.app.use"
    )
    assert indexed.written() == {"proj.m.fallback", "proj.app.identity"}


def test_a_function_without_defaults_references_nothing(indexed: _Indexed) -> None:
    helper = _named(parse(_PY, _PY_SOURCE), cs.TS_PY_FUNCTION_DEFINITION, "helper")
    indexed.walkers.ingest_default_param_references(
        helper, _spec("proj.m.helper"), "proj.m", None, None, "proj.m.helper"
    )
    assert get_relationships(indexed.ingestor) == []


def test_the_module_walk_covers_tables_and_assignments_outside_functions(
    indexed: _Indexed,
) -> None:
    root = parse(_PY, _PY_SOURCE)
    indexed.walkers.ingest_module_references(root, "proj.m", _PY, indexed.queries)
    assert indexed.sources() == {"proj.m"}
    # A dispatch table is treated as calls, in a class body too, which runs
    # at import time like the module's own.
    assert indexed.written(cs.RelationshipType.CALLS) == {
        "proj.m.helper",
        "proj.m.left",
    }
    # A class body's assignment is no module assignment by default, and
    # nothing inside a function or method is the module's.
    assert indexed.written() == {"proj.m.helper"}


def test_class_field_assignments_descend_into_class_bodies_only(
    indexed: _Indexed,
) -> None:
    root = parse(_PY, _PY_SOURCE)
    indexed.walkers.ingest_module_references(
        root, "proj.m", _PY, indexed.queries, class_field_assignments=True
    )
    # The method's local stays the method's.
    assert indexed.written() == {"proj.m.helper", "proj.m.right"}


def test_jsx_references_the_components_and_handlers_it_renders(
    indexed: _Indexed,
) -> None:
    app = _named(parse(_TSX, _TSX_SOURCE), "function_declaration", "App")
    indexed.walkers.ingest_jsx_component_references(
        app,
        _spec("proj.app.App"),
        "proj.app",
        None,
        None,
        indexed.boundaries(_TSX),
        "proj.app.App",
    )
    # A lowercase tag is an HTML element, an interface is never renderable
    # (issue #2535), and a short-circuit in a data prop is data (issue #980).
    assert indexed.written() == {"proj.app.Card", "proj.app.handleClick"}


def test_a_logical_default_references_its_fallback_but_a_comparison_does_not(
    indexed: _Indexed,
) -> None:
    app = _named(parse(_TSX, _TSX_SOURCE), "function_declaration", "App")
    indexed.walkers.ingest_assignment_function_references(
        app,
        _spec("proj.app.App"),
        "proj.app",
        None,
        None,
        indexed.boundaries(_TSX),
        "proj.app.App",
        language=_TSX,
    )
    assert indexed.written() == {"proj.app.fallback"}


def test_an_arrows_expression_body_is_its_return_unless_a_parameter_shadows_it(
    indexed: _Indexed,
) -> None:
    make, ident = nodes_of(parse(_TSX, _TSX_SOURCE), "arrow_function")[-2:]
    for arrow, qn in ((make, "proj.app.make"), (ident, "proj.app.ident")):
        indexed.walkers.ingest_returned_function_references(
            arrow,
            _spec(qn),
            "proj.app",
            None,
            None,
            indexed.boundaries(_TSX),
            qn,
            _TSX,
        )
    # `(noop) => noop` hands back its parameter, not the module's `noop`.
    assert {
        (str(r.source), str(r.target)) for r in get_relationships(indexed.ingestor)
    } == {("proj.app.make", "proj.app.noop")}
