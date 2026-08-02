"""A `#[cfg(test)]` attribute on a Rust mod declaration reaches its target.

The attribute sits on the `mod NAME;` declaration in the declaring file,
while the Module node is minted from the target file itself, so nothing
recorded the gate (issue #1010). Parse time now merges the attribute onto
the TARGET module's decorators: bodyless declarations resolve to the
declared file module (siblings of an entry file, children elsewhere),
bodied inline mods to their own inline node.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)


def _module_decorators(mock_ingestor: MagicMock, qn: str) -> list[str]:
    decorators: list[str] = []
    for call in mock_ingestor.ensure_node_batch.call_args_list:
        label, props = call.args
        if label != cs.NodeLabel.MODULE or props.get(cs.KEY_QUALIFIED_NAME) != qn:
            continue
        value = props.get(cs.KEY_DECORATORS)
        if isinstance(value, list):
            decorators.extend(value)
    return decorators


def test_gate_on_bodyless_declaration_marks_entry_sibling_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's repro: lib.rs declares `#[cfg(test)] mod testutil;` and
    # testutil.rs keys as the entry file's qn sibling.
    project = temp_repo / "rs_cfgtest_sibling"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_sibling"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\nmod testutil;\n\n"
                "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n"
            ),
            "src/testutil.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    decorators = _module_decorators(mock_ingestor, "rs_cfgtest_sibling.src.testutil")
    assert "#[cfg(test)]" in decorators, decorators


def test_gate_on_bodyless_declaration_marks_child_of_plain_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A non-entry file's submodules nest under its qn.
    project = temp_repo / "rs_cfgtest_child"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_child"\nversion = "0.1.0"\n',
            "src/lib.rs": "mod util;\n\npub fn add() -> i32 {\n    1\n}\n",
            "src/util.rs": "#[cfg(test)]\nmod helpers;\n\npub fn go() -> i32 {\n    2\n}\n",
            "src/util/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    decorators = _module_decorators(mock_ingestor, "rs_cfgtest_child.src.util.helpers")
    assert "#[cfg(test)]" in decorators, decorators


def test_gate_on_bodied_inline_module_marks_its_own_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An inline gated mod under a NON-test name records the gate on its
    # inline Module node (the `tests` spelling is already name-matched).
    project = temp_repo / "rs_cfgtest_inline"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_inline"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\n"
                "mod checks {\n"
                "    pub fn fixture() -> i32 {\n        7\n    }\n"
                "}\n\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    decorators = _module_decorators(mock_ingestor, "rs_cfgtest_inline.src.lib.checks")
    assert "#[cfg(test)]" in decorators, decorators


def test_ungated_declarations_carry_no_gate(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    project = temp_repo / "rs_cfgtest_none"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_none"\nversion = "0.1.0"\n',
            "src/lib.rs": "mod util;\n\npub fn add() -> i32 {\n    1\n}\n",
            "src/util.rs": "pub fn go() -> i32 {\n    2\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    assert _module_decorators(mock_ingestor, "rs_cfgtest_none.src.util") == []
