"""A `#[cfg(test)]` attribute on a Rust mod declaration reaches its target.

The attribute sits on the `mod NAME;` declaration in the declaring file,
while the Module node is minted from the target file itself, so nothing
recorded the gate (issue #1010). Parse time records the gate on the
DECLARING file's own Module node as target-qn candidates (a property on
the target's node would be erased whenever an incremental run re-parses
the target file alone), resolved through the same relative-path machinery
`self::` use paths take. Bodied inline mods carry their attributes on
their own inline node.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _write,
    create_and_run_updater,
)


def _module_prop(mock_ingestor: MagicMock, qn: str, key: str) -> list[str]:
    values: list[str] = []
    for call in mock_ingestor.ensure_node_batch.call_args_list:
        label, props = call.args
        if label != cs.NodeLabel.MODULE or props.get(cs.KEY_QUALIFIED_NAME) != qn:
            continue
        value = props.get(key)
        if isinstance(value, list):
            values.extend(value)
    return values


def _declared_gates(mock_ingestor: MagicMock, declaring_qn: str) -> list[str]:
    return _module_prop(mock_ingestor, declaring_qn, cs.KEY_RUST_CFG_TEST_MODS)


def test_gate_on_bodyless_declaration_targets_entry_sibling_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The issue's repro: lib.rs declares `#[cfg(test)] mod testutil;` and
    # testutil.rs keys as the entry file's qn sibling. The record lives on
    # the DECLARING module's node; the target's own node stays untouched
    # (an incremental re-parse of testutil.rs re-mints it, so anything
    # merged there would be silently erased).
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

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_sibling.src.lib")
    assert "rs_cfgtest_sibling.src.testutil" in gates, gates
    assert (
        _module_prop(
            mock_ingestor, "rs_cfgtest_sibling.src.testutil", cs.KEY_DECORATORS
        )
        == []
    )


def test_gate_on_bodyless_declaration_targets_child_of_plain_file(
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

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_child.src.util")
    assert "rs_cfgtest_child.src.util.helpers" in gates, gates


def test_gate_in_non_root_main_file_stays_on_its_own_child(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A plain module file NAMED main.rs is not a crate root: its declared
    # submodules nest under it. The gate must land on cli.main.inner and
    # never on the unrelated production sibling cli.inner.
    project = temp_repo / "rs_cfgtest_collide"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_collide"\nversion = "0.1.0"\n',
            "src/lib.rs": "pub mod cli;\n",
            "src/cli/mod.rs": "pub mod main;\npub mod inner;\n",
            "src/cli/main.rs": "#[cfg(test)]\nmod inner;\n\npub fn run() -> i32 {\n    1\n}\n",
            "src/cli/main/inner.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
            "src/cli/inner.rs": "pub fn production_helper() -> i32 {\n    3\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_collide.src.cli.main")
    assert "rs_cfgtest_collide.src.cli.main.inner" in gates, gates
    assert "rs_cfgtest_collide.src.cli.inner" not in gates, gates


def test_gate_in_explicit_manifest_target_attaches_beside_it(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An explicit `[[bin]] path = "src/tool.rs"` target is a crate root:
    # its declared submodules sit beside it, exactly as its crate:: paths
    # attach.
    project = temp_repo / "rs_cfgtest_bin"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_cfgtest_bin"\nversion = "0.1.0"\n\n'
                '[[bin]]\nname = "tool"\npath = "src/tool.rs"\n'
            ),
            "src/tool.rs": (
                "#[cfg(test)]\nmod support;\n\nfn main() {\n    let _ = 1;\n}\n"
            ),
            "src/support.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_bin.src.tool")
    assert "rs_cfgtest_bin.src.support" in gates, gates


def test_gate_on_bodyless_declaration_inside_inline_mod(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A declaration nested in an inline mod records both spellings the qn
    # scheme can produce (the file-derived child and the inline-nested
    # chain); at most one names a real module (E0428), and dead-code keeps
    # only candidates that do.
    project = temp_repo / "rs_cfgtest_chain"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_chain"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "pub mod outer {\n    #[cfg(test)]\n    pub mod helpers;\n}\n"
            ),
            "src/outer/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_chain.src.lib")
    assert "rs_cfgtest_chain.src.outer.helpers" in gates, gates


def test_gate_survives_interleaved_doc_comment(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A doc comment between the attribute and the declaration is legal and
    # does not detach the gate.
    project = temp_repo / "rs_cfgtest_doc"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_doc"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\n/// Test-only helpers.\nmod testutil;\n\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
            "src/testutil.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_doc.src.lib")
    assert "rs_cfgtest_doc.src.testutil" in gates, gates


def test_path_attribute_declaration_gates_the_file_it_names(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A #[path]-redirected target keys under the FILE the attribute names,
    # so that is the qn the gate records (issue #1035). The name-derived
    # spelling backs nothing and must not be recorded, and no node is
    # minted for either (the fixture audit rejects orphans).
    project = temp_repo / "rs_cfgtest_pathattr"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_cfgtest_pathattr"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": (
                "#[cfg(test)]\n"
                '#[path = "support/helpers.rs"]\n'
                "mod helpers;\n\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
            "src/support/helpers.rs": "pub(crate) fn fixture() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gates = _declared_gates(mock_ingestor, "rs_cfgtest_pathattr.src.lib")
    assert "rs_cfgtest_pathattr.src.support.helpers" in gates, gates
    assert "rs_cfgtest_pathattr.src.helpers" not in gates, gates


def test_gate_on_bodied_inline_module_marks_its_own_node(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An inline gated mod under a NON-test name records its FULL attribute
    # list on its inline Module node (the `tests` spelling is already
    # name-matched).
    project = temp_repo / "rs_cfgtest_inline"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_inline"\nversion = "0.1.0"\n',
            "src/lib.rs": (
                "#[cfg(test)]\n"
                "#[allow(dead_code)]\n"
                "mod checks {\n"
                "    pub fn fixture() -> i32 {\n        7\n    }\n"
                "}\n\n"
                "pub fn add() -> i32 {\n    1\n}\n"
            ),
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    decorators = _module_prop(
        mock_ingestor, "rs_cfgtest_inline.src.lib.checks", cs.KEY_DECORATORS
    )
    assert "#[cfg(test)]" in decorators, decorators
    assert "#[allow(dead_code)]" in decorators, decorators


def test_cross_target_ungated_declaration_is_recorded(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # A gated lib declaration and an ungated bin declaration of the SAME
    # file module: each declarer records its own polarity, so dead-code
    # can let the production declaration win.
    project = temp_repo / "rs_cfgtest_xtarget"
    _write(
        project,
        {
            "Cargo.toml": '[package]\nname = "rs_cfgtest_xtarget"\nversion = "0.1.0"\n',
            "src/lib.rs": "#[cfg(test)]\nmod util;\n\npub fn add() -> i32 {\n    1\n}\n",
            "src/main.rs": "mod util;\n\nfn main() {\n    let _ = util::helper();\n}\n",
            "src/util.rs": "pub(crate) fn helper() -> i32 {\n    7\n}\n",
        },
    )
    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    gated = _declared_gates(mock_ingestor, "rs_cfgtest_xtarget.src.lib")
    assert "rs_cfgtest_xtarget.src.util" in gated, gated
    ungated = _module_prop(
        mock_ingestor, "rs_cfgtest_xtarget.src.main", cs.KEY_RUST_UNGATED_MODS
    )
    assert "rs_cfgtest_xtarget.src.util" in ungated, ungated


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

    assert _declared_gates(mock_ingestor, "rs_cfgtest_none.src.lib") == []
    assert (
        _module_prop(mock_ingestor, "rs_cfgtest_none.src.util", cs.KEY_DECORATORS) == []
    )
