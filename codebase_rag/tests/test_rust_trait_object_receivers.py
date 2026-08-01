# A trait-object/impl-Trait parameter (`s: &dyn Svc`) never entered the
# local variable type map: tree-sitter wraps the trait in dynamic_type /
# abstract_type / bounded_type nodes, none of which the Rust bare-type
# walker descends through, so the receiver stayed untyped and `s.run()`
# fell to the name-only trie fallback whose lexicographic tie-break binds
# an ARBITRARY same-named method (an impl or the trait method, depending
# on how the type names happen to sort). The static callee must be the
# trait method, mirroring the Java interface-receiver design; OVERRIDES
# expansion then keeps every impl alive for dead-code.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tree_sitter import Language, Parser

from codebase_rag import constants as cs
from codebase_rag.parsers.rs import type_inference as rs_ti
from codebase_rag.parsers.rs import utils as rs_utils
from codebase_rag.tests.conftest import run_updater
from evals.dead_code import cgr_dead_code, default_dead_code_config

try:
    import tree_sitter_rust as tsrust

    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# Impl names sort BEFORE "Svc" so the pre-fix lexicographic tie-break
# picks an impl, not the trait method; the fix must not depend on luck.
TRAIT_OBJECT_CALLERS = (
    "trait Svc { fn run(&self) -> i32; }\n"
    "struct Alpha;\n"
    "impl Svc for Alpha { fn run(&self) -> i32 { 1 } }\n"
    "struct Beta;\n"
    "impl Svc for Beta { fn run(&self) -> i32 { 2 } }\n"
    "pub fn use_ref(s: &dyn Svc) -> i32 { s.run() }\n"
    "pub fn use_mut(s: &mut dyn Svc) -> i32 { s.run() }\n"
    "pub fn use_boxed(s: Box<dyn Svc>) -> i32 { s.run() }\n"
    "pub fn use_impl(s: &impl Svc) -> i32 { s.run() }\n"
    "pub fn use_bounded(s: impl Svc + Clone) -> i32 { s.run() }\n"
    "pub fn use_paren(s: &(dyn Svc + Send)) -> i32 { s.run() }\n"
)


def _run_calls(temp_repo: Path, mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    (temp_repo / "m.rs").write_text(TRAIT_OBJECT_CALLERS, encoding="utf-8")
    run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
    return {
        (c.args[0][2], c.args[2][2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS and c.args[2][2].endswith(".run")
    }


@pytest.mark.parametrize(
    "caller",
    ["use_ref", "use_mut", "use_boxed", "use_impl", "use_bounded", "use_paren"],
)
def test_trait_typed_receiver_binds_to_trait_method(
    temp_repo: Path, mock_ingestor: MagicMock, caller: str
) -> None:
    calls = _run_calls(temp_repo, mock_ingestor)
    bound = {callee for c, callee in calls if c.endswith(f".{caller}")}
    assert bound == {f"{temp_repo.name}.m.Svc.run"}, sorted(calls)


def test_boxed_dyn_return_type_binds_to_trait_method(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same walker gap on the RETURN side: a factory returning
    # `Box<dyn Svc>` typed its result as `Box`, so a call on the bound
    # local never reached the trait method either. (An associated-fn
    # factory: only impl-method return types are recorded, a free fn's
    # `let s = make()` chain is a separate, unrelated gap.)
    (temp_repo / "m.rs").write_text(
        "trait Svc { fn run(&self) -> i32; }\n"
        "struct Alpha;\n"
        "impl Svc for Alpha { fn run(&self) -> i32 { 1 } }\n"
        "struct Beta;\n"
        "impl Svc for Beta { fn run(&self) -> i32 { 2 } }\n"
        "struct Maker;\n"
        "impl Maker { fn make() -> Box<dyn Svc> { Box::new(Alpha) } }\n"
        "pub fn use_made() -> i32 { let s = Maker::make(); s.run() }\n",
        encoding="utf-8",
    )
    run_updater(temp_repo, mock_ingestor, skip_if_missing="rust")
    bound = {
        c.args[2][2]
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == cs.RelationshipType.CALLS
        and c.args[0][2].endswith(".use_made")
        and c.args[2][2].endswith(".run")
    }
    assert bound == {f"{temp_repo.name}.m.Svc.run"}, sorted(bound)


def test_multi_impl_trait_nothing_dead_regardless_of_names(tmp_path: Path) -> None:
    # With impls sorting before the trait, the pre-fix arbitrary binding
    # lands on one impl, leaving the trait method and the other impl dead.
    # Typed to the trait, the call plus OVERRIDES expansion keeps all
    # three alive.
    root = tmp_path / "rdyn"
    root.mkdir()
    (root / "m.rs").write_text(
        "trait Svc { fn run(&self) -> i32; }\n"
        "struct Alpha;\n"
        "impl Svc for Alpha { fn run(&self) -> i32 { 1 } }\n"
        "struct Beta;\n"
        "impl Svc for Beta { fn run(&self) -> i32 { 2 } }\n"
        "pub fn use_it(s: &dyn Svc) -> i32 { s.run() }\n",
        encoding="utf-8",
    )
    dead = cgr_dead_code(root, "proj", default_dead_code_config(False, False))
    assert not [d for d in dead if d.endswith(".run")], sorted(dead)


@pytest.mark.skipif(not RUST_AVAILABLE, reason="tree-sitter-rust not installed")
def test_one_ary_tuple_is_not_grouping_parens() -> None:
    # Rust writes a 1-ary tuple `(T,)` with a trailing comma; it also has
    # exactly one typed child, so a child-count-only grouping check would
    # type the value as its element. Only comma-free parens are grouping.
    parser = Parser(Language(tsrust.language()))
    src = (
        b"fn a(s: (Alpha,)) -> (Beta,) { s.run() }\n"
        b"fn b(s: (Alpha)) -> (Beta) { s.run() }\n"
    )
    fn_tuple, fn_grouped = parser.parse(src).root_node.children
    engine = rs_ti.RustTypeInferenceEngine()
    assert "s" not in engine.build_local_variable_type_map(fn_tuple, "proj.m")
    assert rs_utils.extract_return_type_name(fn_tuple, None) is None
    grouped = engine.build_local_variable_type_map(fn_grouped, "proj.m")
    assert grouped.get("s") == "Alpha"
    assert rs_utils.extract_return_type_name(fn_grouped, None) == "Beta"
