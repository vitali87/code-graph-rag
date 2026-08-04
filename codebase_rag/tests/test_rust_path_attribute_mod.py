"""A `#[path]` attribute redirects a Rust mod declaration to another file.

The qn scheme is path-derived, so the target keys under where the FILE
sits, while every declaration-side resolution used to compute the
name-derived spelling: a qn no module owns. Both consumers now read the
attribute, so a `#[cfg(test)]` gate reaches the file it names and a crate
path through the declared name lands on that file too (issue #1035).
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.test_rust_cfg_test_mod_declarations import _declared_gates
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_path_attribute_gate_names_the_target_file_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `#[path = "support/helpers.rs"] mod helpers;` in src/lib.rs backs
    # src/support/helpers.rs, which indexes as src.support.helpers. The
    # name-derived src.helpers owns nothing, so recording it leaves the
    # gate inert and `fixture` reads as production code.
    project = temp_repo / "rs_path_attr"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_path_attr"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "support/helpers.rs"]\n'
                "mod helpers;\n"
                "\n"
                "pub fn add(a: i32, b: i32) -> i32 {\n"
                "    a + b\n"
                "}\n"
            ),
            "src/support/helpers.rs": ("pub(crate) fn fixture() -> i32 {\n    7\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr.src.lib")
    assert "rs_path_attr.src.support.helpers" in gates, gates


def test_path_attribute_target_beside_a_plain_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The path is relative to the DIRECTORY holding the declaring file,
    # never to a directory named after it, so a plain (non mod-rs) file
    # redirects its declaration into its own parent directory.
    project = temp_repo / "rs_path_attr_plain"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_plain"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": (
                "#[cfg(test)]\n"
                '#[path = "fixtures/rig.rs"]\n'
                "mod rig;\n"
                "\n"
                "pub fn run() -> i32 {\n"
                "    1\n"
                "}\n"
            ),
            "src/fixtures/rig.rs": "pub(crate) fn build() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_plain.src.engine")
    assert "rs_path_attr_plain.src.fixtures.rig" in gates, gates


def test_declaration_without_a_path_attribute_is_untouched(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The ordinary spelling still resolves through the relative-path
    # machinery; only a redirected declaration takes the file-derived qn.
    project = temp_repo / "rs_path_attr_none"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_none"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "#[cfg(test)]\nmod helpers;\n\npub fn add() -> i32 {\n    1\n}\n",
            "src/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_none.src.lib")
    assert "rs_path_attr_none.src.helpers" in gates, gates
    assert not any(cs.SEPARATOR_DOT + "support" in gate for gate in gates), gates


def test_crate_path_resolves_through_a_redirected_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `crate::helpers::fixture()` names the module by its DECLARED name
    # while the module keys under the redirected file, so the crate path
    # has to land on the file's qn to reach anything at all.
    project = temp_repo / "rs_path_attr_call"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_call"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                '#[path = "support/helpers.rs"]\n'
                "mod helpers;\n"
                "pub mod a;\n"
                "pub mod decoy;\n"
            ),
            "src/support/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": ("pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n"),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    assert (
        "rs_path_attr_call.src.a.run",
        "rs_path_attr_call.src.support.helpers.fixture",
    ) in calls, calls
    assert (
        "rs_path_attr_call.src.a.run",
        "rs_path_attr_call.src.decoy.fixture",
    ) not in calls, calls


def test_a_commented_out_redirect_does_not_hijack_a_live_declaration(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The declaration scan reads comment-and-string-blanked source so that
    # no comment or literal can present a declaration. The `#[path]` value
    # has to survive that blanking to be read at all, and keeping it must
    # not reopen the door: an attribute inside a comment is not one.
    project = temp_repo / "rs_path_attr_comment"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_comment"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "/* previous layout:\n"
                '#[path = "decoy.rs"]\n'
                "mod helpers;\n"
                "*/\n"
                "mod helpers;\n"
                "pub mod decoy;\n"
                "pub mod a;\n"
            ),
            "src/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_comment.src.a.run"
    assert (caller, "rs_path_attr_comment.src.helpers.fixture") in calls, calls
    assert (caller, "rs_path_attr_comment.src.decoy.fixture") not in calls, calls


def test_a_redirect_inside_a_string_literal_is_not_read(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A raw string may hold whole lines of Rust; none of it is code.
    project = temp_repo / "rs_path_attr_string"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_string"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                'pub const SAMPLE: &str = r#"\n'
                '#[path = "decoy.rs"]\n'
                "mod helpers;\n"
                '"#;\n'
                "mod helpers;\n"
                "pub mod decoy;\n"
                "pub mod a;\n"
            ),
            "src/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_string.src.a.run"
    assert (caller, "rs_path_attr_string.src.helpers.fixture") in calls, calls
    assert (caller, "rs_path_attr_string.src.decoy.fixture") not in calls, calls


def test_a_redirect_declared_inside_a_macro_body_is_read(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Platform modules are routinely declared inside `cfg_if!`, and the
    # declaration scan keeps depth-0 macro bodies for exactly that reason,
    # so a redirect written there is a real one.
    project = temp_repo / "rs_path_attr_macro"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_path_attr_macro"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "pub mod a;\n"
                "cfg_if! {\n"
                '    #[path = "support/helpers.rs"]\n'
                "    mod helpers;\n"
                "}\n"
            ),
            "src/support/helpers.rs": "pub fn fixture() -> i32 {\n    7\n}\n",
            "src/decoy.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    calls = _calls(mock_ingestor)
    caller = "rs_path_attr_macro.src.a.run"
    assert (
        caller,
        "rs_path_attr_macro.src.support.helpers.fixture",
    ) in calls, calls


def test_an_absolute_redirect_claims_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An absolute path names a file outside the indexed tree, so both the
    # gate and the crate path stand down rather than inventing a spelling.
    project = temp_repo / "rs_path_attr_abs"
    _write(
        project,
        {
            "Cargo.toml": ('[package]\nname = "rs_path_attr_abs"\nversion = "0.1.0"\n'),
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "/nowhere/helpers.rs"]\n'
                "mod helpers;\n"
                "\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")
    gates = _declared_gates(mock_ingestor, "rs_path_attr_abs.src.lib")
    assert not any("nowhere" in gate for gate in gates), gates
