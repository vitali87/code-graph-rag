# (H) Regression: a DEFINES_METHOD relationship is matched in the graph by the
# (H) parent's LABEL and qualified_name, so a method on a non-Class container
# (H) (a Rust trait -> Interface node) must be emitted with the parent's real
# (H) label. It was hardcoded to Class, so MATCH (a:Class {qn: trait}) found
# (H) nothing and the trait -> method containment edge was silently dropped.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import NodeLabel, RelationshipType
from codebase_rag.tests.conftest import create_and_run_updater, get_relationships


def test_rust_trait_method_defined_by_interface_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_trait"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_trait"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(
        encoding="utf-8",
        data="""pub trait Shape {
    fn area(&self) -> f64 { 0.0 }
}
""",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    defines_method = get_relationships(
        mock_ingestor, RelationshipType.DEFINES_METHOD.value
    )
    # (H) (parent_label, parent_qn) pairs for the trait's method.
    parents = {
        (call[0][0][0], call[0][0][2])
        for call in defines_method
        if str(call[0][2][2]).endswith(".Shape.area")
    }
    assert (NodeLabel.INTERFACE.value, "rs_trait.src.lib.Shape") in parents, parents
    # (H) The wrong Class-labelled parent must not be emitted.
    assert (NodeLabel.CLASS.value, "rs_trait.src.lib.Shape") not in parents, parents
