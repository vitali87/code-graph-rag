"""Workspace member crate names resolve to project qns in Rust use paths.

A `use` whose head names another workspace crate kept its raw `::` path in
the import mapping: only crate::/super::/self:: paths were rewritten at
parse time, so a cross-crate call reached its target only by trie luck and
an external import shadowing a first-party sibling still yielded a
fabricated edge (issue #1033: ripgrep's `use grep_searcher::sinks;` then
`sinks::UTF8(...)`).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_glob_member_module_qualified_call_binds_cross_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Members declared by glob; the crate name is hyphenated, spoken with
    # underscores in code. sinks::utf8() must bind the searcher crate's
    # function, never the caller crate's same-named decoy.
    project = temp_repo / "rs_ws_glob"
    _write(
        project,
        {
            "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
            "crates/searcher/Cargo.toml": (
                '[package]\nname = "grep-searcher"\nversion = "0.1.0"\n'
            ),
            "crates/searcher/src/lib.rs": "pub mod sinks;\n",
            "crates/searcher/src/sinks.rs": "pub fn utf8() -> i32 {\n    1\n}\n",
            "crates/core/Cargo.toml": '[package]\nname = "core"\nversion = "0.1.0"\n',
            "crates/core/src/main.rs": (
                "mod decoy;\n\n"
                "use grep_searcher::sinks;\n\n"
                "fn main() {\n    let _ = sinks::utf8();\n}\n"
            ),
            "crates/core/src/decoy.rs": "pub fn utf8() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = "rs_ws_glob.crates.core.src.main.main"
    assert (caller, "rs_ws_glob.crates.searcher.src.sinks.utf8") in calls, calls
    assert (caller, "rs_ws_glob.crates.core.src.decoy.utf8") not in calls, calls


def test_listed_member_direct_item_import_binds(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Members listed literally; `use util::helper;` imports an item
    # declared in the member's lib.rs itself.
    project = temp_repo / "rs_ws_listed"
    _write(
        project,
        {
            "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\n',
            "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\n',
            "util/src/lib.rs": "pub fn helper() -> i32 {\n    1\n}\n",
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "app/src/main.rs": (
                "mod decoy;\n\n"
                "use util::helper;\n\n"
                "fn main() {\n    let _ = helper();\n}\n"
            ),
            "app/src/decoy.rs": "pub fn helper() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = "rs_ws_listed.app.src.main.main"
    assert (caller, "rs_ws_listed.util.src.lib.helper") in calls, calls
    assert (caller, "rs_ws_listed.app.src.decoy.helper") not in calls, calls


def test_root_package_name_binds_integration_test_import(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # No [workspace] at all: an integration test (tests/*.rs is its own
    # crate) imports the root package's lib BY NAME, the only way Rust
    # allows it to.
    project = temp_repo / "rs_ws_root"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "my-lib"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub fn engine_run() -> i32 {\n    1\n}\n",
            "tests/integration.rs": (
                "use my_lib::engine_run;\n\n"
                "#[test]\nfn t() {\n    let _ = engine_run();\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    assert (
        "rs_ws_root.tests.integration.t",
        "rs_ws_root.src.lib.engine_run",
    ) in calls, calls


def test_lib_path_override_roots_member_crate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A member whose lib target is repointed by `[lib] path`: the crate
    # name maps to the named file, not to a src/lib.rs that is absent.
    project = temp_repo / "rs_ws_libpath"
    _write(
        project,
        {
            "Cargo.toml": '[workspace]\nmembers = ["engine", "app"]\n',
            "engine/Cargo.toml": (
                '[package]\nname = "engine"\nversion = "0.1.0"\n\n'
                '[lib]\npath = "src/custom.rs"\n'
            ),
            "engine/src/custom.rs": "pub fn spin() -> i32 {\n    1\n}\n",
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "app/src/main.rs": (
                "use engine::spin;\n\nfn main() {\n    let _ = spin();\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    assert (
        "rs_ws_libpath.app.src.main.main",
        "rs_ws_libpath.engine.src.custom.spin",
    ) in calls, calls


def test_external_import_shadowing_sibling_is_dropped(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use std::fmt;` binds `fmt` to std in worker.rs; the sibling
    # src/fmt.rs must not receive the call by bare-name fallback. With
    # workspace crates resolved at parse time, an import-bound head that
    # still resolves to no project qn is genuinely external: a decided
    # drop, not a trie guess.
    project = temp_repo / "rs_ws_shadow"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "shadow"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod fmt;\nmod worker;\n\nfn main() {\n"
                "    let _ = worker::run();\n    let _ = fmt::format();\n}\n"
            ),
            "src/worker.rs": (
                "use std::fmt;\n\n"
                "pub fn run() -> String {\n"
                '    fmt::format(format_args!("hi"))\n'
                "}\n"
            ),
            "src/fmt.rs": "pub fn format() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    assert (
        "rs_ws_shadow.src.worker.run",
        "rs_ws_shadow.src.fmt.format",
    ) not in calls, calls
    # main's own unshadowed module-qualified call keeps its edge.
    assert (
        "rs_ws_shadow.src.main.main",
        "rs_ws_shadow.src.fmt.format",
    ) in calls, calls


def test_local_module_head_beats_member_crate_name(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # app has its own `mod util;` and does not depend on the member crate
    # of the same name: rustc binds the uniform-path head to the local
    # module, so the member's name must not hijack it.
    project = temp_repo / "rs_ws_localwins"
    _write(
        project,
        {
            "Cargo.toml": '[workspace]\nmembers = ["app", "util"]\n',
            "util/Cargo.toml": '[package]\nname = "util"\nversion = "0.1.0"\n',
            "util/src/lib.rs": "pub fn helper() -> i32 {\n    1\n}\n",
            "app/Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "app/src/main.rs": (
                "mod util;\n\n"
                "use util::helper;\n\n"
                "fn main() {\n    let _ = helper();\n}\n"
            ),
            "app/src/util.rs": "pub fn helper() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = "rs_ws_localwins.app.src.main.main"
    assert (caller, "rs_ws_localwins.app.src.util.helper") in calls, calls
    assert (caller, "rs_ws_localwins.util.src.lib.helper") not in calls, calls
