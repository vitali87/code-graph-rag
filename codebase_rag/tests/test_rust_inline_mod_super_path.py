"""A `super::` path inside an inline `mod` must count from the written path.

`_resolve_rust_prefixed_path` declined every caller nested below the file
module, so the call fell to the enclosing-scope walk, which matches the tail by
NAME against each ancestor of the FILE's own qn. That is right whenever the file
sits where its module sits, and wrong the moment a `#[path]` moved it: the walk
answers with whatever same-named module happens to sit in a physical ancestor
(issue #1086).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'


def test_super_super_from_an_inline_mod_in_a_path_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `rig` is crate::engine::rig, so `super::super` from `inner` is `engine`.
    # src/fixtures/sib.rs is declared by nobody and only sits in a physical
    # ancestor of the file, which is exactly what the name walk used to grab.
    name = "rs_inline_super_path"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                '#[path = "fixtures/rig.rs"]\npub mod rig;\npub mod sib;\n'
            ),
            "src/engine/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "pub mod inner {\n"
                "    pub fn build() -> i32 {\n"
                "        super::super::sib::run()\n"
                "    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.fixtures.rig.inner.build"
    assert (caller, f"{name}.src.engine.sib.run") in calls, calls
    assert (caller, f"{name}.src.fixtures.sib.run") not in calls, calls


def test_single_super_from_an_inline_mod_reaches_the_file_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # One `super::` from an inline mod is the FILE module, not its parent.
    name = "rs_inline_super_one"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "pub fn helper() -> i32 {\n    1\n}\n"
                "pub mod inner {\n"
                "    pub fn build() -> i32 {\n        super::helper()\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.inner.build"
    assert (caller, f"{name}.src.engine.helper") in calls, calls


def test_self_from_an_inline_mod_stays_in_the_inline_mod(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `self::` names the inline mod itself, so the same-named file-module item
    # must not win.
    name = "rs_inline_self"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "pub fn helper() -> i32 {\n    1\n}\n"
                "pub mod inner {\n"
                "    pub fn helper() -> i32 {\n        9\n    }\n"
                "    pub fn build() -> i32 {\n        self::helper()\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.inner.build"
    assert (caller, f"{name}.src.engine.inner.helper") in calls, calls
    assert (caller, f"{name}.src.engine.helper") not in calls, calls


def test_cfg_test_inline_mod_super_reaches_the_file_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[cfg(test)] mod tests { ... super::... }` is the shape that reaches
    # this most often.
    name = "rs_inline_cfg_tests"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "pub fn helper() -> i32 {\n    1\n}\n"
                "#[cfg(test)]\n"
                "mod tests {\n"
                "    #[test]\n"
                "    fn works() {\n        super::helper();\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.engine.tests.works"
    assert (caller, f"{name}.src.engine.helper") in calls, calls


def test_nested_inline_mods_pop_one_level_each(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_inline_nested"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "pub fn helper() -> i32 {\n    1\n}\n"
                "pub mod outer {\n"
                "    pub fn helper() -> i32 {\n        2\n    }\n"
                "    pub mod inner {\n"
                "        pub fn build() -> i32 {\n"
                "            super::helper()\n"
                "        }\n"
                "        pub fn climb() -> i32 {\n"
                "            super::super::helper()\n"
                "        }\n"
                "    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    build = f"{name}.src.engine.outer.inner.build"
    climb = f"{name}.src.engine.outer.inner.climb"
    assert (build, f"{name}.src.engine.outer.helper") in calls, calls
    assert (climb, f"{name}.src.engine.helper") in calls, calls
