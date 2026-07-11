# (H) Two adjacent gaps left open by the trait-object receiver fix: a free
# (H) Rust fn's return type was never recorded in method_return_types (only
# (H) impl methods were, in class ingest), and a bare `let s = make()`
# (H) single-segment chain bailed out of call-return inference. Together they
# (H) left a free-function factory's result untyped, so a call on it fell to
# (H) the name-only trie fallback (or was dropped as external) instead of
# (H) binding the trait method.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.rs import type_inference as rs_ti
from codebase_rag.tests.conftest import run_updater
from evals.dead_code import cgr_dead_code, default_dead_code_config

try:
    import tree_sitter_rust as tsrust

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

TRAIT_AND_IMPLS = (
    "trait Svc { fn run(&self) -> i32; }\n"
    "struct Alpha;\n"
    "impl Svc for Alpha { fn run(&self) -> i32 { 1 } }\n"
    "struct Beta;\n"
    "impl Svc for Beta { fn run(&self) -> i32 { 2 } }\n"
)

FREE_FN_FACTORY = TRAIT_AND_IMPLS + (
    "pub fn make() -> Box<dyn Svc> { Box::new(Alpha) }\n"
    "pub fn try_make() -> Result<Box<dyn Svc>, ()> { Ok(make()) }\n"
    "pub fn use_made() -> i32 { let s = make(); s.run() }\n"
    "pub fn use_tried() -> i32 { let s = try_make().unwrap(); s.run() }\n"
)


def _run_calls(
    temp_repo: Path, mock_ingestor: MagicMock, files: dict[str, str]
) -> set[tuple[str, str]]:
    for name, source in files.items():
        (temp_repo / name).write_text(source, encoding="utf-8")
    run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS and c.args[2][2].endswith(".run")
    }


@pytest.mark.parametrize("caller", ["use_made", "use_tried"])
def test_free_fn_factory_result_binds_to_trait_method(
    temp_repo: Path, mock_ingestor: MagicMock, caller: str
) -> None:
    calls = _run_calls(temp_repo, mock_ingestor, {"m.rs": FREE_FN_FACTORY})
    bound = {callee for c, callee in calls if c.endswith(f".{caller}")}
    assert bound == {f"{temp_repo.name}.m.Svc.run"}, sorted(calls)


def test_imported_free_fn_factory_result_binds_to_trait_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # (H) The factory lives in another module and is brought in by `use`; the
    # (H) bare-name call must resolve through the import's `::` path to the
    # (H) function's registry qn before the return-type lookup.
    files = {
        "factory.rs": TRAIT_AND_IMPLS
        + "pub fn make() -> Box<dyn Svc> { Box::new(Alpha) }\n",
        "m.rs": (
            "use crate::factory::{make, Svc};\n"
            "pub fn use_made() -> i32 { let s = make(); s.run() }\n"
        ),
    }
    calls = _run_calls(temp_repo, mock_ingestor, files)
    bound = {callee for c, callee in calls if c.endswith(".use_made")}
    assert bound == {f"{temp_repo.name}.factory.Svc.run"}, sorted(calls)


def test_free_fn_factory_keeps_all_impls_alive(tmp_path: Path) -> None:
    root = tmp_path / "rfree"
    root.mkdir()
    (root / "m.rs").write_text(
        TRAIT_AND_IMPLS
        + "pub fn make() -> Box<dyn Svc> { Box::new(Alpha) }\n"
        + "pub fn use_made() -> i32 { let s = make(); s.run() }\n",
        encoding="utf-8",
    )
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert not [d for d in dead if d.endswith(".run")], sorted(dead)


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not installed")
def test_bare_identifier_binding_is_not_a_call() -> None:
    # (H) `let f = make;` is a move/fn-pointer binding, not a call: `f` holds
    # (H) the function itself, not a value of its return type, so no chain
    # (H) binding may be collected for it. Only an invoked base qualifies.
    parser = Parser(Language(tsrust.language()))
    src = b"fn user() { let f = make; let s = make(); }\n"
    fn_node = parser.parse(src).root_node.children[0]
    bindings = dict(rs_ti.RustTypeInferenceEngine().collect_call_var_bindings(fn_node))
    assert "f" not in bindings, bindings
    assert bindings.get("s") == ["make"]
