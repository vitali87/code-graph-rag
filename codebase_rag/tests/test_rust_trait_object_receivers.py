# (H) A trait-object/impl-Trait parameter (`s: &dyn Svc`) never entered the
# (H) local variable type map: tree-sitter wraps the trait in dynamic_type /
# (H) abstract_type / bounded_type nodes, none of which the Rust bare-type
# (H) walker descends through, so the receiver stayed untyped and `s.run()`
# (H) fell to the name-only trie fallback whose lexicographic tie-break binds
# (H) an ARBITRARY same-named method (an impl or the trait method, depending
# (H) on how the type names happen to sort). The static callee must be the
# (H) trait method, mirroring the Java interface-receiver design; OVERRIDES
# (H) expansion then keeps every impl alive for dead-code.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.conftest import run_updater
from evals.dead_code import cgr_dead_code, default_dead_code_config

# (H) Impl names sort BEFORE "Svc" so the pre-fix lexicographic tie-break
# (H) picks an impl, not the trait method; the fix must not depend on luck.
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
    # (H) The same walker gap on the RETURN side: a factory returning
    # (H) `Box<dyn Svc>` typed its result as `Box`, so a call on the bound
    # (H) local never reached the trait method either.
    (temp_repo / "m.rs").write_text(
        "trait Svc { fn run(&self) -> i32; }\n"
        "struct Alpha;\n"
        "impl Svc for Alpha { fn run(&self) -> i32 { 1 } }\n"
        "struct Beta;\n"
        "impl Svc for Beta { fn run(&self) -> i32 { 2 } }\n"
        "fn make() -> Box<dyn Svc> { Box::new(Alpha) }\n"
        "pub fn use_made() -> i32 { let s = make(); s.run() }\n",
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
    # (H) With impls sorting before the trait, the pre-fix arbitrary binding
    # (H) lands on one impl, leaving the trait method and the other impl dead.
    # (H) Typed to the trait, the call plus OVERRIDES expansion keeps all
    # (H) three alive.
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
