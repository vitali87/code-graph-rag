"""`RustFunctionIngest`: a body-local item held back until the module item
sharing its name has claimed it, a free function's qualified name and
return type, the REFERENCES a closure gets from its scope, and the inline
module an item is defined by."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.rust.function_ingest import RustFunctionIngest
from codebase_rag.tests.collaborator_support import edges, first, index, parse
from codebase_rag.tests.conftest import get_nodes
from codebase_rag.tests.rust_fixture import RUST_CRATE, function_named
from evals.recorded_graph import RecordingIngestor

_RS = cs.SupportedLanguage.RUST
_CLASH = "rp.src.clash"


@pytest.fixture
def ingest(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RustFunctionIngest:
    updater = index(temp_repo / "rp", mock_ingestor, RUST_CRATE)
    rust = updater.factory.definition_processor.function_ingest.rust
    assert isinstance(rust, RustFunctionIngest)
    return rust


def test_a_body_local_item_leaves_the_shared_name_to_the_module_item(
    ingest: RustFunctionIngest, mock_ingestor: RecordingIngestor
) -> None:
    returns = {
        node.properties[cs.KEY_QUALIFIED_NAME]: node.properties.get(cs.KEY_RETURN_TYPE)
        for node in get_nodes(mock_ingestor, cs.NodeLabel.FUNCTION)
    }
    # Written first, inside `outer`, yet the module item keeps the name.
    assert returns[f"{_CLASH}.tally"] == "u8"
    assert returns[f"{_CLASH}.tally@9"] == "String"
    assert ingest.facts.method_return_types[f"{_CLASH}.tally"] == "u8"
    assert ingest.facts.method_return_types[f"{_CLASH}.tally@9"] == "String"
    defines = edges(mock_ingestor, cs.RelationshipType.DEFINES)
    assert {(_CLASH, f"{_CLASH}.tally"), (f"{_CLASH}.outer", f"{_CLASH}.tally@9")} <= (
        defines
    )
    calls = {t for s, t in edges(mock_ingestor, cs.RelationshipType.CALLS)}
    assert f"{_CLASH}.tally@9" in calls
    assert f"{_CLASH}.tally" not in calls


def test_an_item_inside_a_body_is_body_local(ingest: RustFunctionIngest) -> None:
    root = parse(_RS, RUST_CRATE["src/lib.rs"])
    assert ingest.is_body_local(function_named(root, "local"))
    assert not ingest.is_body_local(function_named(root, "run"))
    assert not ingest.is_body_local(function_named(root, "helper"))


def test_an_item_in_an_inline_mod_is_named_and_defined_by_it() -> None:
    root = parse(_RS, RUST_CRATE["src/lib.rs"])
    helper = function_named(root, "helper")
    run = function_named(root, "run")
    qualified = RustFunctionIngest.qualified_name
    assert qualified(helper, "rp.src.lib", "helper") == "rp.src.lib.util.helper"
    assert qualified(run, "rp.src.lib", "run") == "rp.src.lib.run"
    assert RustFunctionIngest.inline_module_parent(
        helper, "rp.src.lib.util.helper"
    ) == (
        cs.NodeLabel.MODULE,
        "rp.src.lib.util",
        None,
    )
    assert RustFunctionIngest.inline_module_parent(run, "rp.src.lib.run") is None


def test_a_free_functions_return_type_is_its_head_name(
    ingest: RustFunctionIngest,
) -> None:
    for source, qn in (
        ("fn g() -> Vec<u8> { vec![] }", "x.g"),
        ("fn k() -> Option<Circle> { None }", "x.k"),
        # No impl target for a free fn, so `Self` names nothing.
        ("fn s() -> Self { todo!() }", "x.s"),
        ("fn u() {}", "x.u"),
    ):
        ingest.record_return_type(first(parse(_RS, source), "function_item"), qn)
    recorded = ingest.facts.method_return_types
    assert {qn: t for qn, t in recorded.items() if qn.startswith("x.")} == {
        "x.g": "Vec",
        "x.k": "Circle",
    }


def test_a_closure_is_referenced_by_the_scope_it_is_written_in(
    ingest: RustFunctionIngest, mock_ingestor: RecordingIngestor
) -> None:
    closure = ("rp.src.lib.run", "rp.src.lib.anonymous_12_14")
    assert closure in edges(mock_ingestor, cs.RelationshipType.DEFINES)
    # A named body-local item gets no such edge: its calls keep it live.
    assert edges(mock_ingestor, cs.RelationshipType.REFERENCES) == {closure}
