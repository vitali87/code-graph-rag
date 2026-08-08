"""`super::`/`self::` calls must climb, not bind in the caller's own module.

`_try_resolve_via_imports` answers a `super::item()` call with the item of that
name in the caller's OWN module and never honours the prefix, and it runs before
the Rust module-qualified probe that knows how to climb (issue #1093). The
failure only surfaces when both modules hold the name; with only the outer one
present the call drops instead, which is cheaper but still not rustc's edge.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'


def test_super_call_from_an_impl_block_climbs_one_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_super_impl"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/engine/thing.rs": (
                "pub struct S;\n"
                "pub fn helper() -> i32 {\n    9\n}\n"
                "impl S {\n"
                "    pub fn build() -> i32 {\n        super::helper()\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.thing.S.build"
    assert (caller, f"{name}.src.engine.helper") in calls, calls
    assert (caller, f"{name}.src.engine.thing.helper") not in calls, calls


def test_super_call_from_a_free_function_climbs_one_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The impl block is incidental: it is the `super::` prefix reaching the
    # import probe that decides, not the enclosing scope.
    name = "rs_super_free"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/engine/thing.rs": (
                "pub fn helper() -> i32 {\n    9\n}\n"
                "pub fn build() -> i32 {\n    super::helper()\n}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.thing.build"
    assert (caller, f"{name}.src.engine.helper") in calls, calls
    assert (caller, f"{name}.src.engine.thing.helper") not in calls, calls


def test_self_call_binds_in_the_callers_own_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `self::helper()` names THIS module's item, so the same-named outer one
    # must not win either.
    name = "rs_self_prefix"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/engine/thing.rs": (
                "pub fn helper() -> i32 {\n    9\n}\n"
                "pub fn build() -> i32 {\n    self::helper()\n}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.thing.build"
    assert (caller, f"{name}.src.engine.thing.helper") in calls, calls
    assert (caller, f"{name}.src.engine.helper") not in calls, calls


def test_super_call_with_no_local_shadow_still_climbs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # With only the outer item present the call used to drop rather than bind
    # wrongly; it must reach the parent module either way.
    name = "rs_super_no_shadow"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/engine/thing.rs": (
                "pub fn build() -> i32 {\n    super::helper()\n}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.thing.build"
    assert (caller, f"{name}.src.engine.helper") in calls, calls


def test_use_super_item_declaration_still_resolves(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use super::item;` resolves through the import half and is out of scope
    # for this fix; pinned so the reordering does not disturb it.
    name = "rs_use_super"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/engine/thing.rs": (
                "use super::helper;\npub fn build() -> i32 {\n    helper()\n}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.thing.build"
    assert (caller, f"{name}.src.engine.helper") in calls, calls


def test_super_from_an_impl_on_an_unregistered_type_still_climbs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `impl Trait for u8` has no Class node, so a scope that is merely absent
    # from the type registry cannot be assumed to be an inline mod: doing so
    # left `super::` one level short and bound the call inside this file.
    name = "rs_super_primitive_impl"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod area;\npub fn helper() -> i32 {\n    1\n}\n",
            "src/area.rs": (
                "pub fn helper() -> i32 {\n    9\n}\n"
                "pub trait LocalTrait {\n    fn run(&self) -> i32;\n}\n"
                "impl LocalTrait for u8 {\n"
                "    fn run(&self) -> i32 {\n        super::helper()\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.area.u8.run"
    assert (caller, f"{name}.src.lib.helper") in calls, calls
    assert (caller, f"{name}.src.area.helper") not in calls, calls
