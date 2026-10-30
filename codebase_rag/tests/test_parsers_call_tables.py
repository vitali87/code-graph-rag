"""`TableReferences`: the functions a scope's dict, object, list and array
literals hold as values. A named value is treated as a call from the scope
that builds the table, an inline function is found by the name or position
the definition pass registered it under, and the walk stops at nested
scopes except an anonymous callback no caller pass covers."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from tree_sitter import Node

from codebase_rag import constants as cs
from codebase_rag.parsers.call_processor import CallProcessor
from codebase_rag.parsers.call_tables import TableReferences
from codebase_rag.parsers.scope_boundaries import flow_scope_boundaries
from codebase_rag.tests.collaborator_support import first, index, nodes_of, parse
from codebase_rag.tests.conftest import get_relationships
from codebase_rag.types_defs import LanguageQueries
from evals.recorded_graph import RecordingIngestor

pytestmark = pytest.mark.usefixtures("fingerprint_inputs_once_per_session")

_PY = cs.SupportedLanguage.PYTHON
_JS = cs.SupportedLanguage.JS
_PY_SOURCE = """\
def helper(): pass
def other(): pass
def chosen(): pass
def default(): pass
def is_set(): pass
def compute(): pass
def nested_only(): pass
def deep(): pass

def build():
    table = {"a": helper, "c": compute(), "k": (chosen if is_set else default)}
    queue = [other, [[deep]]]
    def inner():
        return {"n": nested_only}
    return table
"""
_JS_SOURCE = """\
function retryStrategy() {}
function handleError() {}
function compute() {}
function handler() {}
export function build(toast, items) {
  items.forEach(() => { const t = { h: handler }; });
  const opts = {
    retryStrategy,
    onError: handleError.bind(toast),
    onSuccess: () => compute(),
    onCreated: (s) => { s.setEvents({ inner: () => 1 }); },
    'quoted': () => 2,
    named: function getrouter() {},
    then(x) { return x; },
    label: compute(),
  };
  return opts;
}
"""


@dataclass
class _Indexed:
    processor: CallProcessor
    queries: Mapping[cs.SupportedLanguage, LanguageQueries]
    ingestor: RecordingIngestor

    @property
    def tables(self) -> TableReferences:
        return self.processor.tables

    def walk(self, language: cs.SupportedLanguage, source: str, qn: str) -> None:
        # The walk over one caller, as its language's call pass runs it.
        node_type = (
            cs.TS_PY_FUNCTION_DEFINITION if language == _PY else "function_declaration"
        )
        self.tables.ingest_collection_function_references(
            _named(parse(language, source), node_type, qn.rsplit(".", 1)[-1]),
            _spec(qn),
            qn.rsplit(".", 1)[0],
            None,
            None,
            flow_scope_boundaries(self.queries[language][cs.QUERY_CONFIG]),
            language,
        )

    def written(self, rel_type: str) -> Counter[str]:
        return Counter(
            str(rel.target) for rel in get_relationships(self.ingestor, rel_type)
        )


@pytest.fixture
def indexed(temp_repo: Path, mock_ingestor: RecordingIngestor) -> _Indexed:
    updater = index(
        temp_repo / "proj", mock_ingestor, {"p.py": _PY_SOURCE, "t.js": _JS_SOURCE}
    )
    # Only what the walk under test writes is under test.
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


def test_python_table_values_are_calls_from_the_scope_that_builds_them(
    indexed: _Indexed,
) -> None:
    indexed.walk(_PY, _PY_SOURCE, "proj.p.build")
    # Each once: a nested literal is expanded by its outermost container
    # alone (issue #2262). Both results of a wrapped ternary; never its
    # condition, a call's result, or the nested def's own table.
    assert indexed.written(cs.RelationshipType.CALLS) == Counter(
        {
            "proj.p.helper": 1,
            "proj.p.other": 1,
            "proj.p.chosen": 1,
            "proj.p.default": 1,
            "proj.p.deep": 1,
        }
    )
    assert {str(rel.source) for rel in get_relationships(indexed.ingestor)} == {
        "proj.p.build"
    }


def test_a_named_js_value_is_referenced_through_its_shorthand_or_bind(
    indexed: _Indexed,
) -> None:
    indexed.walk(_JS, _JS_SOURCE, "proj.t.build")
    calls = indexed.written(cs.RelationshipType.CALLS)
    # `.bind(toast)` hands `handleError` over; `label: compute()` stores a
    # result, not the function.
    assert "proj.t.handleError" in calls
    assert "proj.t.compute" not in calls
    assert "proj.t.retryStrategy" in indexed.written(cs.RelationshipType.REFERENCES)


def test_an_inline_js_value_is_found_where_the_definition_pass_registered_it(
    indexed: _Indexed,
) -> None:
    indexed.walk(_JS, _JS_SOURCE, "proj.t.build")
    references = set(indexed.written(cs.RelationshipType.REFERENCES))
    assert {
        # By its key, by its position under a string key, by its own name
        # and its key for a named function expression, and a shorthand
        # method by its name.
        "proj.t.build.onSuccess",
        "proj.t.build.anonymous_11_14",
        "proj.t.build.getrouter",
        "proj.t.build.named",
        "proj.t.build.then",
        # A value under another pair's arrow registers under the key path.
        "proj.t.build.onCreated.inner",
    } <= references


def test_a_table_in_an_anonymous_callback_belongs_to_the_enclosing_scope(
    indexed: _Indexed,
) -> None:
    indexed.walk(_JS, _JS_SOURCE, "proj.t.build")
    # The `forEach` arrow gets no caller pass, so its table is this scope's.
    assert indexed.written(cs.RelationshipType.CALLS)["proj.t.handler"] == 1


def test_a_value_names_a_function_only_as_a_bare_name(indexed: _Indexed) -> None:
    build = _named(parse(_PY, _PY_SOURCE), cs.TS_PY_FUNCTION_DEFINITION, "build")
    helper = first(build, cs.TS_PY_PAIR).child_by_field_name(cs.FIELD_VALUE)
    assert helper is not None and helper.text == b"helper"
    # `compute()` in value position stores the call's result.
    call = first(build, cs.TS_PY_CALL)
    site = first(parse(_PY, "x\n"), cs.TS_PY_IDENTIFIER)
    indexed.processor.site_node = site
    for value in (helper, call):
        indexed.tables.emit_value_function_ref(
            value,
            _spec("proj.p.build"),
            "proj.p",
            None,
            None,
            indexed.processor._resolver.resolve_function_call,
            indexed.processor.emit_rel,
            "proj.p.build",
        )
    (edge,) = get_relationships(indexed.ingestor)
    assert (edge.rel_type, edge.target) == (cs.RelationshipType.CALLS, "proj.p.helper")
    # The edge carries the value's own site; the caller's is put back.
    assert edge.properties[cs.KEY_LINE] == 11
    assert indexed.processor.site_node is site
