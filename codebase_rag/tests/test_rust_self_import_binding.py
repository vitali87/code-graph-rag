"""A brace-list `self` import binds the module under its own name.

`use crate::io::{self, Reader};` puts the module in scope as `io`, but the
import mapping keyed the entry on the `self` keyword, a name nothing ever
looks up, so every later `io::...` spelling in the file resolved to nothing
(issue #1054).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_brace_self_import_binds_module_qualified_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::io::{self, Reader};` puts the MODULE in scope as `io`, so a
    # later `io::open()` resolves through it. Keyed on the `self` keyword the
    # binding is unreachable and the call falls to the trie, which happily
    # picks the caller's own same-named decoy.
    project = temp_repo / "rs_self_import"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod io;\npub mod app;\npub mod decoy;\n",
            "src/io.rs": ("pub struct Reader;\n\npub fn open() -> i32 {\n    1\n}\n"),
            "src/decoy.rs": "pub fn open() -> i32 {\n    2\n}\n",
            "src/app.rs": (
                "use crate::io::{self, Reader};\n"
                "\n"
                "pub fn run() -> i32 {\n"
                "    let _r = Reader;\n"
                "    io::open()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_self_import.src.app.run"
    assert (caller, "rs_self_import.src.io.open") in calls, calls
    assert (caller, "rs_self_import.src.decoy.open") not in calls, calls


def test_nested_brace_self_import_binds_module_qualified_call(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The same `self` nested one level deeper (`use crate::{io::{self}, util}`),
    # where the base path is assembled from two enclosing lists rather than a
    # single scoped prefix.
    project = temp_repo / "rs_self_nested"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod io;\npub mod util;\npub mod app;\npub mod decoy;\n",
            "src/io.rs": "pub fn open() -> i32 {\n    1\n}\n",
            "src/util.rs": "pub fn helper() -> i32 {\n    3\n}\n",
            "src/decoy.rs": "pub fn open() -> i32 {\n    2\n}\n",
            "src/app.rs": (
                "use crate::{io::{self}, util};\n"
                "\n"
                "pub fn run() -> i32 {\n"
                "    let _ = util::helper();\n"
                "    io::open()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_self_nested.src.app.run"
    assert (caller, "rs_self_nested.src.io.open") in calls, calls
    assert (caller, "rs_self_nested.src.decoy.open") not in calls, calls


def test_self_alias_import_still_binds_under_the_alias(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `use crate::io::{self as sys};` renames the module. The alias form takes
    # a different code path, so binding the plain form must not disturb it: the
    # module answers to `sys`, and to nothing else.
    project = temp_repo / "rs_self_alias"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "app"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod io;\npub mod app;\npub mod decoy;\n",
            "src/io.rs": "pub fn open() -> i32 {\n    1\n}\n",
            "src/decoy.rs": "pub fn open() -> i32 {\n    2\n}\n",
            "src/app.rs": (
                "use crate::io::{self as sys};\n"
                "\n"
                "pub fn run() -> i32 {\n"
                "    sys::open()\n"
                "}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_self_alias.src.app.run"
    assert (caller, "rs_self_alias.src.io.open") in calls, calls
    assert (caller, "rs_self_alias.src.decoy.open") not in calls, calls
