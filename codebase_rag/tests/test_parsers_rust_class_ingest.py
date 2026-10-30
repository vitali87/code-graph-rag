"""`RustClassIngest`: the type an impl block's methods join, the trait an
impl implements and its methods override, which impls only another crate's
dispatch can call, where a re-exported trait is declared, a struct's field
types and the Module node an inline `mod` gets."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.parsers.rust.class_ingest import RustClassIngest
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.tests.conftest import get_nodes
from codebase_rag.tests.rust_fixture import RUST_CRATE
from codebase_rag.types_defs import DeferredInherit
from evals.recorded_graph import RecordingIngestor

_SHAPES = "rp.src.shapes"
_CIRCLE = f"{_SHAPES}.Circle"
_SQUARE = "rp.src.clash.Square"
_AREA = f"{_SHAPES}.Area"


@pytest.fixture
def ingest(temp_repo: Path, mock_ingestor: RecordingIngestor) -> RustClassIngest:
    updater = index(temp_repo / "rp", mock_ingestor, RUST_CRATE)
    rust = updater.factory.definition_processor.class_ingest.rust
    assert isinstance(rust, RustClassIngest)
    return rust


def _parent_of(parent_qn: str) -> DeferredInherit:
    return DeferredInherit(
        cs.RelationshipType.IMPLEMENTS,
        _SQUARE,
        parent_qn,
        "rp.src.clash",
        0,
        cs.SupportedLanguage.RUST,
    )


def test_an_impl_blocks_methods_join_the_type_it_targets(
    ingest: RustClassIngest, mock_ingestor: RecordingIngestor
) -> None:
    methods = edges(mock_ingestor, cs.RelationshipType.DEFINES_METHOD)
    assert {t for s, t in methods if s == _CIRCLE} == {
        f"{_CIRCLE}.area",
        f"{_CIRCLE}.new",
        f"{_CIRCLE}.fmt",
    }
    assert ingest.impl_owner_module == {_CIRCLE: _SHAPES, _SQUARE: "rp.src.clash"}
    # Only the inherent impl's method is no trait's.
    assert ingest.inherent_impl_methods == {f"{_CIRCLE}.new"}


def test_a_trait_impl_implements_and_overrides_the_trait_it_names(
    ingest: RustClassIngest, mock_ingestor: RecordingIngestor
) -> None:
    assert edges(mock_ingestor, cs.RelationshipType.IMPLEMENTS) == {
        (_CIRCLE, _AREA),
        # `fmt::Display` expands through the file's `use std::fmt;`.
        (_CIRCLE, "std.fmt.Display"),
        # `crate::Area` names the root's re-export; the trait is declared in
        # shapes.
        (_SQUARE, _AREA),
    }
    assert edges(mock_ingestor, cs.RelationshipType.OVERRIDES) == {
        (f"{_CIRCLE}.area", f"{_AREA}.area"),
        (f"{_SQUARE}.area", f"{_AREA}.area"),
    }
    assert ingest.impl_method_traits == {
        f"{_CIRCLE}.area": _AREA,
        f"{_SQUARE}.area": _AREA,
    }


def test_only_an_external_traits_methods_are_flagged_as_overriding_it(
    ingest: RustClassIngest, mock_ingestor: RecordingIngestor
) -> None:
    flagged = {
        node.properties[cs.KEY_QUALIFIED_NAME]
        for node in get_nodes(mock_ingestor, cs.NodeLabel.METHOD)
        if node.properties.get(cs.KEY_OVERRIDES_EXTERNAL)
    }
    assert flagged == {f"{_CIRCLE}.fmt"}


def test_a_re_exported_parent_resolves_where_it_is_declared(
    ingest: RustClassIngest,
) -> None:
    assert ingest.reexport_target(_parent_of("rp.src.lib.Area")) == _AREA
    assert ingest.reexport_target(_parent_of("rp.src.lib.Missing")) is None


def test_two_modules_re_exporting_each_other_declare_nothing(
    ingest: RustClassIngest,
) -> None:
    mapping = ingest.import_processor.import_mapping
    mapping["rp.src.a"] = {"Loop": "rp.src.b.Loop"}
    mapping["rp.src.b"] = {"Loop": "rp.src.a.Loop"}
    assert ingest.reexport_target(_parent_of("rp.src.a.Loop")) is None


def test_a_structs_field_types_are_recorded(ingest: RustClassIngest) -> None:
    assert ingest.facts.class_field_types[_CIRCLE] == {"r": "f64"}


def test_an_inline_mod_is_a_module_inside_its_file(
    ingest: RustClassIngest, mock_ingestor: RecordingIngestor
) -> None:
    modules = {
        node.properties[cs.KEY_QUALIFIED_NAME]: node.properties
        for node in get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        if cs.KEY_NAME in node.properties
    }
    util = modules["rp.src.lib.util"]
    assert (util[cs.KEY_NAME], util[cs.KEY_PATH]) == ("util", "src/lib.rs")
    assert (util[cs.KEY_START_LINE], util[cs.KEY_END_LINE]) == (6, 8)
    defines = edges(mock_ingestor, cs.RelationshipType.DEFINES)
    assert ("rp.src.lib", "rp.src.lib.util") in defines
    assert ("rp.src.lib.util", "rp.src.lib.util.helper") in defines


def test_a_files_mod_declarations_are_recorded_as_ungated(
    ingest: RustClassIngest, mock_ingestor: RecordingIngestor
) -> None:
    ungated = {
        node.properties[cs.KEY_QUALIFIED_NAME]: node.properties[
            cs.KEY_RUST_UNGATED_MODS
        ]
        for node in get_nodes(mock_ingestor, cs.NodeLabel.MODULE)
        if cs.KEY_RUST_UNGATED_MODS in node.properties
    }
    assert ungated == {
        "rp.src.lib": ["rp.src.clash", "rp.src.shapes"],
        _SHAPES: ["rp.src.shapes.inner"],
    }
