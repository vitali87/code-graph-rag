# Rust closures nested in an impl-method body must get a DEFINES edge from
# the enclosing METHOD, exactly as closures in free functions get one from
# the enclosing function. cgr used to derive the closure's DEFINES parent via
# the FQN scope walk, which could not read an impl block's target type, so the
# parent endpoint dropped the impl target (`lib.run` instead of `lib.Foo.run`)
# and never matched the real Method node, silently dropping the containment.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.constants import KEY_QUALIFIED_NAME, NodeLabel, RelationshipType
from codebase_rag.tests.conftest import (
    create_and_run_updater,
    get_nodes,
    get_relationships,
)

_RS = """pub struct Foo;

impl Foo {
    pub fn run(&self) -> i32 {
        let c = |x: i32| x + 1;
        c(2)
    }
}

pub fn free() -> i32 {
    let d = |y: i32| y + 2;
    d(3)
}
"""


def _project(temp_repo: Path) -> Path:
    project = temp_repo / "rs_clo"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text(
        encoding="utf-8", data='[package]\nname = "rs_clo"\nversion = "0.1.0"\n'
    )
    (project / "src" / "lib.rs").write_text(encoding="utf-8", data=_RS)
    return project


def _defines_pairs(mock_ingestor: MagicMock) -> set[tuple[str, str, str]]:
    # (parent_label, parent_qn, child_qn) for DEFINES edges.
    return {
        (call[0][0][0], call[0][0][2], call[0][2][2])
        for call in get_relationships(mock_ingestor, RelationshipType.DEFINES.value)
    }


def test_rust_closure_in_impl_method_defined_by_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    create_and_run_updater(_project(temp_repo), mock_ingestor, skip_if_missing="rust")
    file_mod = "rs_clo.src.lib"

    method_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.METHOD)
    }
    assert f"{file_mod}.Foo.run" in method_qns, method_qns

    function_qns = {
        str(node[0][1].get(KEY_QUALIFIED_NAME))
        for node in get_nodes(mock_ingestor, NodeLabel.FUNCTION)
    }

    pairs = _defines_pairs(mock_ingestor)
    # Every DEFINES edge's parent endpoint must resolve to a real node;
    # the method-closure edge used to point at the phantom `lib.run`.
    method_defines = {
        (parent_qn, child_qn)
        for (parent_label, parent_qn, child_qn) in pairs
        if parent_label == NodeLabel.METHOD.value
    }
    assert method_defines, pairs
    closure_child = next(
        child_qn
        for (parent_qn, child_qn) in method_defines
        if parent_qn == f"{file_mod}.Foo.run"
    )
    assert closure_child in function_qns, (closure_child, function_qns)
