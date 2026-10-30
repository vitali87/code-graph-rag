"""`RustFileIngest`: what a Rust file settles once its function and class
passes have run, a function body's `use`s and its block-local items keyed
on the qn the registry handed out, and nothing for an item that never
registered."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parsers.rust.file_ingest import RustFileIngest
from codebase_rag.tests.collaborator_support import edges, index, parse
from codebase_rag.tests.rust_fixture import RUST_CRATE
from evals.recorded_graph import RecordingIngestor

_RS = cs.SupportedLanguage.RUST
_HELPER = "rp.src.lib.util.helper"


@pytest.fixture
def updater(temp_repo: Path, mock_ingestor: RecordingIngestor) -> GraphUpdater:
    return index(temp_repo / "rp", mock_ingestor, RUST_CRATE)


def _file_ingest(updater: GraphUpdater) -> RustFileIngest:
    rust = updater.factory.definition_processor._for(_RS)
    assert isinstance(rust, RustFileIngest)
    return rust


def test_a_function_bodys_use_keys_on_its_registered_qn(
    updater: GraphUpdater, mock_ingestor: RecordingIngestor
) -> None:
    rust = _file_ingest(updater).import_processor.rust
    assert rust.rust_fn_scope_imports == {"rp.src.clash.outer": {"helper": _HELPER}}
    assert ("rp.src.clash.outer", _HELPER) in edges(
        mock_ingestor, cs.RelationshipType.CALLS
    )


def test_block_local_items_key_on_the_qn_each_was_handed(
    updater: GraphUpdater,
) -> None:
    rust = _file_ingest(updater).import_processor.rust
    items = {
        module: [scope[2] for scope in scopes]
        for module, scopes in rust.rust_block_items.items()
    }
    assert items == {
        # The body-local `tally` lost its name to the module item.
        "rp.src.clash": [{"tally": "rp.src.clash.tally@9"}],
        "rp.src.lib": [{"local": "rp.src.lib.local"}],
    }
    assert rust.rust_block_item_qns == {"rp.src.clash.tally@9", "rp.src.lib.local"}


def test_an_item_that_never_registered_records_no_block_scope(
    updater: GraphUpdater,
) -> None:
    file_ingest = _file_ingest(updater)
    root = parse(_RS, "fn z() { fn hidden() {} }\n")
    file_ingest.finalise(root, "rp.src.ghost", _RS, updater.queries)
    assert "rp.src.ghost" not in file_ingest.import_processor.rust.rust_block_items


def test_settling_a_file_twice_changes_nothing(updater: GraphUpdater) -> None:
    file_ingest = _file_ingest(updater)
    rust = file_ingest.import_processor.rust
    before = (dict(rust.rust_fn_scope_imports), dict(rust.rust_block_items))
    root = parse(_RS, RUST_CRATE["src/clash.rs"])
    file_ingest.finalise(root, "rp.src.clash", _RS, updater.queries)
    assert (dict(rust.rust_fn_scope_imports), dict(rust.rust_block_items)) == before
