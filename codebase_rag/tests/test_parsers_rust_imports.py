"""`RustImporter`: what a file's `use`s bind, which crate heads the manifest
and the repository speak for, how `crate::`, `self::` and `super::` paths
rewrite to qns, and what dropping a file's import state takes with it."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.rust.imports import RustImporter
from codebase_rag.tests.collaborator_support import edges, index
from codebase_rag.tests.rust_fixture import RUST_CRATE
from evals.recorded_graph import RecordingIngestor

_LIB = "rp.src.lib"
_INNER = "rp.src.shapes.inner"


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "rp", mock_ingestor, RUST_CRATE)


@pytest.fixture
def rust(updater: GraphUpdater) -> RustImporter:
    importer = updater.factory.import_processor.rust
    assert isinstance(importer, RustImporter)
    return importer


def test_a_files_uses_bind_their_names(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    mapping = updater.factory.import_processor.import_mapping
    assert mapping[_LIB] == {
        # A grouped `pub use` binds each name.
        "Area": "rp.src.shapes.Area",
        "Circle": "rp.src.shapes.Circle",
        "helper": "rp.src.lib.util.helper",
    }
    # A toolchain path stays as written.
    assert mapping["rp.src.shapes"] == {"fmt": "std::fmt"}
    # A function body's use binds in that function's scope, not the file's.
    assert mapping["rp.src.clash"] == {}
    assert edges(mock_ingestor, cs.RelationshipType.IMPORTS) == {
        (_LIB, "rp.src.lib.util"),
        (_LIB, "rp.src.shapes"),
        ("rp.src.clash", "rp.src.lib.util"),
        ("rp.src.shapes", "std"),
    }


def test_the_manifest_and_the_repository_say_who_a_head_is(
    rust: RustImporter,
) -> None:
    assert rust.rust_head_is_external_dep("serde", _LIB)
    assert not rust.rust_head_is_external_dep("rand", _LIB)
    assert rust.rust_head_is_repo_crate("rp")
    assert not rust.rust_head_is_repo_crate("serde")
    assert not rust.rust_head_is_member_dep("serde", _LIB)


def test_super_climbs_one_module_per_step(rust: RustImporter) -> None:
    assert rust.rust_super_base(_INNER, 1) == "rp.src.shapes"
    assert rust.rust_super_base(_INNER, 2) == "rp.src"


def test_a_relative_use_path_rewrites_to_its_qn(rust: RustImporter) -> None:
    rewrite = rust.rewrite_rust_local_use_path
    assert rewrite("crate::util::helper", _LIB) == "rp.src.lib.util.helper"
    assert rewrite("self::util::helper", _LIB) == "rp.src.lib.util.helper"
    assert rewrite("super::Circle", _INNER) == "rp.src.shapes.Circle"
    # A path under another crate is not this crate's to rewrite.
    assert rewrite("std::fmt", _LIB) == "std::fmt"
    assert (
        rust.rust_resolve_relative(_LIB, ["shapes", "Circle"], _LIB)
        == "rp.src.shapes.Circle"
    )
    assert rust.rust_crate_reaches(_LIB, "rp.src.shapes.Circle")


def test_dropping_a_files_state_takes_only_that_files_scopes(
    rust: RustImporter,
) -> None:
    rust.drop_rust_module_import_state("rp.src.clash")
    assert rust.rust_fn_scope_imports == {}
    assert "rp.src.clash" not in rust.rust_block_items
    assert rust.rust_block_item_qns == {"rp.src.lib.local"}
    assert _LIB in rust.rust_block_items
