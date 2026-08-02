"""Rust `module::item` calls resolve through the module's own namespace.

A module-qualified call names its function through the module path, so it
must bind inside that module: a direct item, or the module's re-export
(`pub use`), and NEVER an unrelated same-named function found by bare-name
search (issue #1009: ripgrep's `flags::parse()` bound `config.parse`).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_module_qualified_call_follows_mod_rs_reexport(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # main calls flags::parse(); flags/mod.rs re-exports parse::parse.
    # The re-export is the module's namespace entry for `parse`, so the
    # edge lands on flags.parse.parse; config.rs's unrelated parse must
    # not be bound by the simple-name fallback.
    project = temp_repo / "rs_modqual_reexport"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modqual_reexport"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod flags;\n\nfn main() {\n    let _ = flags::parse();\n}\n"
            ),
            "src/flags/mod.rs": (
                "pub(crate) mod config;\n"
                "pub(crate) mod parse;\n\n"
                "pub(crate) use crate::flags::parse::{ParseResult, parse};\n"
            ),
            "src/flags/parse.rs": (
                "pub(crate) struct ParseResult;\n\n"
                "pub(crate) fn parse() -> i32 {\n    parse_low()\n}\n\n"
                "fn parse_low() -> i32 {\n    1\n}\n"
            ),
            "src/flags/config.rs": "pub(crate) fn parse() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modqual_reexport.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.main.main", f"{base}.flags.parse.parse") in calls, calls
    assert (f"{base}.main.main", f"{base}.flags.config.parse") not in calls, calls


def test_module_qualified_call_binds_direct_submodule_function(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # util::go() from main binds util.rs's own go; the same-named decoy in
    # another module must not be reachable through the bare-name fallback.
    project = temp_repo / "rs_modqual_direct"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modqual_direct"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod util;\nmod decoy;\n\nfn main() {\n    let _ = util::go();\n}\n"
            ),
            "src/util.rs": "pub fn go() -> i32 {\n    1\n}\n",
            "src/decoy.rs": "pub fn go() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modqual_direct.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.main.main", f"{base}.util.go") in calls, calls
    assert (f"{base}.main.main", f"{base}.decoy.go") not in calls, calls


def test_module_qualified_call_with_deep_path(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # flags::defs::build() names the function through two module segments.
    project = temp_repo / "rs_modqual_deep"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modqual_deep"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "mod flags;\n\nfn main() {\n    let _ = flags::defs::build();\n}\n"
            ),
            "src/flags/mod.rs": "pub mod defs;\n",
            "src/flags/defs.rs": "pub fn build() -> i32 {\n    3\n}\n",
            "src/other.rs": "",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modqual_deep.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.main.main", f"{base}.flags.defs.build") in calls, calls


def test_associated_function_call_still_binds_the_type(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Type::assoc() shares the :: syntax; a registered type's associated
    # function keeps its class binding untouched by the module probe.
    project = temp_repo / "rs_modqual_assoc"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_modqual_assoc"\nversion = "0.1.0"\n',
            "src/main.rs": (
                "struct Ping;\n\n"
                "impl Ping {\n"
                "    fn new() -> Self {\n        Ping\n    }\n"
                "}\n\n"
                "fn main() {\n"
                "    let _ = Ping::new();\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    base = "rs_modqual_assoc.src"
    calls = _calls(mock_ingestor)
    assert (f"{base}.main.main", f"{base}.main.Ping.new") in calls, calls
