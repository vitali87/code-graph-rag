# Rust nested-module containment. cgr qualifies items inside `mod inner`
# with the module path (proj...inner.X), but used to (a) DEFINE them from the
# FILE module while leaving the inner Module node an orphan, and (b) qualify
# impl methods inside the mod against the file module, producing a phantom
# DEFINES_METHOD parent that never matched the real type node. Containment
# must be module-nested: file module -> inner module -> its items, and an
# impl method binds to the type under its enclosing module path.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_QUALIFIED_NAME, NodeLabel, RelationshipType
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)

_RS = """pub mod inner {
    pub fn helper() -> i32 { 1 }

    pub struct Widget { w: i32 }

    impl Widget {
        pub fn build(&self) -> i32 { self.w }
    }
}
"""


def _project(temp_repo: Path) -> Path:
    project = temp_repo / "rs_mod"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_mod"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(encoding="utf-8", data=_RS)
    return project


def _defines_pairs(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    # (parent_label, parent_qn, child_qn) for DEFINES edges.
    return {
        (call[0][0][0], call[0][0][2], call[0][2][2])
        for call in get_relationships(mock_ingestor, RelationshipType.DEFINES.value)
    }


def test_rust_nested_module_is_module_nested(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(_project(temp_repo), mock_ingestor, skip_if_missing="rust")
    file_mod = "rs_mod.src.lib"
    inner = f"{file_mod}.inner"
    pairs = _defines_pairs(mock_ingestor)

    # file module DEFINES the inner module (no longer an orphan node).
    assert (NodeLabel.MODULE.value, file_mod, inner) in pairs, pairs
    # inner module DEFINES its own items, not the file module.
    assert (NodeLabel.MODULE.value, inner, f"{inner}.helper") in pairs, pairs
    assert (NodeLabel.MODULE.value, inner, f"{inner}.Widget") in pairs, pairs


def test_rust_impl_method_in_module_binds_to_nested_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(_project(temp_repo), mock_ingestor, skip_if_missing="rust")
    inner = "rs_mod.src.lib.inner"

    method_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.METHOD)
    }
    assert f"{inner}.Widget.build" in method_qns, method_qns

    defines_method = {
        (call[0][0][2], call[0][2][2])
        for call in get_relationships(
            mock_ingestor, RelationshipType.DEFINES_METHOD.value
        )
    }
    assert (f"{inner}.Widget", f"{inner}.Widget.build") in defines_method, (
        defines_method
    )
