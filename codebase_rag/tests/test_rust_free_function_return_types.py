# Two adjacent gaps left open by the trait-object receiver fix: a free
# Rust fn's return type was never recorded in method_return_types (only
# impl methods were, in class ingest), and a bare `let s = make()`
# single-segment chain bailed out of call-return inference. Together they
# left a free-function factory's result untyped, so a call on it fell to
# the name-only trie fallback (or was dropped as external) instead of
# binding the trait method.
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
    # The factory lives in another module and is brought in by `use`; the
    # bare-name call must resolve through the import's `::` path to the
    # function's registry qn before the return-type lookup.
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
    # `let f = make;` is a move/fn-pointer binding, not a call: `f` holds
    # the function itself, not a value of its return type, so no chain
    # binding may be collected for it. Only an invoked base qualifies.
    parser = Parser(Language(tsrust.language()))
    src = b"fn user() { let f = make; let s = make(); }\n"
    fn_node = parser.parse(src).root_node.children[0]
    collected = rs_ti.RustTypeInferenceEngine().collect_call_var_bindings(fn_node)
    bindings = {name: segments for name, segments, _point in collected}
    assert "f" not in bindings, bindings
    assert bindings.get("s") == ["make"]
    # The CALL's own offset rides along, so a shadowing block item can be told
    # from the module item of that name (issue #1069). The `let` starts earlier
    # and the two differ once the call sits in a nested block.
    assert [point for _name, _segments, point in collected] == [src.index(b"make()")], (
        collected
    )


# The block item returns Zzz and the module item Aaa, so the name-only trie
# fallback an untyped local falls to would answer Aaa: only reading the block
# item's OWN return type gets Zzz, and the assertion can tell the two apart.
BODY_LOCAL_FACTORY = (
    "pub struct Aaa;\n"
    "pub struct Zzz;\n"
    "impl Aaa { pub fn run(&self) -> u32 { 1 } }\n"
    "impl Zzz { pub fn run(&self) -> u32 { 2 } }\n"
    "pub fn make() -> Aaa { Aaa }\n"
    "pub fn outer() -> u32 {\n"
    "    fn make() -> Zzz { Zzz }\n"
    "    let t = make();\n"
    "    t.run()\n"
    "}\n"
    "pub fn sibling() -> u32 {\n"
    "    let u = make();\n"
    "    u.run()\n"
    "}\n"
)


def test_body_local_factory_types_from_its_own_return(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A body-local `fn make` shadows the module's own for the whole of the
    # body it is written in, and it registers under a `@<line>` variant
    # because the module item owns the natural qn. The return-type lookup
    # keyed by name alone could only ever read the module item, so the local
    # bound from the shadowing fn typed as the module fn's return (#1069).
    calls = _run_calls(temp_repo, mock_ingestor, {"m.rs": BODY_LOCAL_FACTORY})
    base = f"{temp_repo.name}.m"
    assert (f"{base}.outer", f"{base}.Zzz.run") in calls, calls
    assert (f"{base}.outer", f"{base}.Aaa.run") not in calls, calls
    # The module item's own callers are unaffected by the shadowing.
    assert (f"{base}.sibling", f"{base}.Aaa.run") in calls, calls
    assert (f"{base}.sibling", f"{base}.Zzz.run") not in calls, calls


def test_unit_returning_shadow_does_not_fall_through_to_the_module_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A shadow with no recorded return type records nothing at all: a unit
    # return has no type name. The site still names the block item, so the
    # module item's return is not the answer either, and typing the local
    # from it fabricates a call on a value that was never of that type.
    source = (
        "pub struct Aaa;\n"
        "pub struct Zed;\n"
        "impl Aaa { pub fn run(&self) -> u32 { 1 } }\n"
        "impl Zed { pub fn run(&self) -> u32 { 2 } }\n"
        "pub fn make() -> Zed { Zed }\n"
        "pub fn outer() {\n"
        "    fn make() { }\n"
        "    let u = make();\n"
        "    u.run();\n"
        "}\n"
    )
    calls = _run_calls(temp_repo, mock_ingestor, {"m.rs": source})
    base = f"{temp_repo.name}.m"
    assert (f"{base}.outer", f"{base}.Zed.run") not in calls, calls


def test_generic_returning_shadow_does_not_fall_through_to_the_module_item(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A generic return records the type PARAMETER, which resolves to no class
    # here. Recording it would let the known-external guard delete the edge the
    # trie fallback still finds, so it types nothing rather than the module
    # item's own return.
    source = (
        "pub struct Aaa;\n"
        "pub struct Zed;\n"
        "impl Aaa { pub fn run(&self) -> u32 { 1 } }\n"
        "impl Zed { pub fn run(&self) -> u32 { 2 } }\n"
        "pub fn make() -> Zed { Zed }\n"
        "pub fn outer() -> u32 {\n"
        "    fn make<X: Default>() -> X { X::default() }\n"
        "    let t = make::<Aaa>();\n"
        "    t.run()\n"
        "}\n"
    )
    calls = _run_calls(temp_repo, mock_ingestor, {"m.rs": source})
    base = f"{temp_repo.name}.m"
    # Naming Zed after Aaa separates the two ways this can go wrong: falling
    # through to the module item types the local Zed, while recording the
    # unresolvable parameter deletes the edge outright. Neither is the
    # untyped local the trie then answers for.
    assert (f"{base}.outer", f"{base}.Aaa.run") in calls, calls
    assert (f"{base}.outer", f"{base}.Zed.run") not in calls, calls
