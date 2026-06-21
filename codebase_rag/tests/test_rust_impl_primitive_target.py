# (H) Regression: methods in an `impl Trait for <primitive>` block (e.g.
# (H) `impl From<Foo> for u8`) must be captured. The impl target `u8` is a
# (H) `primitive_type` node, which extract_impl_target did not recognise, so every
# (H) method in such a block was silently dropped.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_QUALIFIED_NAME, NodeLabel
from codebase_rag.tests.conftest import create_and_run_updater, get_nodes


def test_rust_method_on_primitive_impl_target_is_captured(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_prim"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_prim"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(
        encoding="utf-8",
        data="""pub enum Foo { A, B }

impl From<Foo> for u8 {
    fn from(value: Foo) -> Self {
        match value {
            Foo::A => 0,
            Foo::B => 1,
        }
    }
}
""",
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    method_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.METHOD)
    }
    assert any(qn.endswith(".u8.from") for qn in method_qns), (
        f"from() on impl-for-u8 not captured: {method_qns}"
    )
