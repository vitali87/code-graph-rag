"""An unrepresentable `#[path]` target must claim nothing at all.

`path_attribute_qn_parts` returns None for a target the qn scheme cannot key
(an absolute path, a Windows separator, a climb above the repo root). Crate-path
resolution treated that exactly like a declaration with NO redirect and fell
back to the name-derived module, binding an undeclared sibling that happens to
sit where the declared name points (issue #1082). The gate side already stands
down for these; the crate-path side did not.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'


def _corpus(name: str, redirect: str) -> dict[str, str]:
    return {
        "Cargo.toml": CARGO.format(name=name),
        "src/lib.rs": (f'#[path = "{redirect}"]\nmod helpers;\npub mod a;\n'),
        "src/helpers.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
        "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
    }


def _helper_edges(mock_ingestor: MagicMock, name: str) -> set[tuple[str, str]]:
    return {
        pair
        for pair in _calls(mock_ingestor)
        if pair[0] == f"{name}.src.a.run" and "helpers" in pair[1]
    }


def test_absolute_redirect_binds_no_shadow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/helpers.rs is declared by nobody; the module is backed by a file
    # outside the indexed tree, so nothing in the graph is its referent.
    name = "rs_path_absolute"
    project = temp_repo / name
    _write(project, _corpus(name, "/nowhere/helpers.rs"))

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    assert _helper_edges(mock_ingestor, name) == set()


def test_windows_separator_redirect_binds_no_shadow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_path_windows"
    project = temp_repo / name
    _write(project, _corpus(name, "..\\\\outside\\\\helpers.rs"))

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    assert _helper_edges(mock_ingestor, name) == set()


def test_climb_above_repo_root_binds_no_shadow(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_path_climb"
    project = temp_repo / name
    _write(project, _corpus(name, "../../../outside/helpers.rs"))

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    assert _helper_edges(mock_ingestor, name) == set()


def test_a_representable_redirect_still_binds_its_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: an in-tree redirect must keep working, keying under the file
    # it names rather than the declared module name.
    name = "rs_path_ok"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": (
                '#[path = "fixtures/helpers.rs"]\nmod helpers;\npub mod a;\n'
            ),
            "src/fixtures/helpers.rs": "pub fn fixture() -> i32 {\n    2\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    assert (f"{name}.src.a.run", f"{name}.src.fixtures.helpers.fixture") in calls, calls


def test_no_redirect_still_binds_the_named_module(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: a declaration with no `#[path]` at all is the case the buggy
    # branch conflated the unrepresentable target with. It must be unaffected.
    name = "rs_path_none"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "mod helpers;\npub mod a;\n",
            "src/helpers.rs": "pub fn fixture() -> i32 {\n    1\n}\n",
            "src/a.rs": "pub fn run() -> i32 {\n    crate::helpers::fixture()\n}\n",
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    assert (f"{name}.src.a.run", f"{name}.src.helpers.fixture") in calls, calls


def _implements(mock_ingestor: MagicMock) -> set[tuple[str, str]]:
    return {
        (call[0][0][2], call[0][2][2])
        for call in mock_ingestor.ensure_relationship_batch.call_args_list
        if call[0][1] == "IMPLEMENTS"
    }


def test_trait_path_through_an_unrepresentable_redirect_binds_no_trait(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `crate::helpers::Base` runs through a redirect the qn scheme cannot key.
    # Falling back to the name-anchored candidate would hand the same-named
    # indexed trait in src/other.rs an IMPLEMENTS edge it has no claim to.
    name = "rs_path_trait"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": (
                '#[path = "/nowhere/helpers.rs"]\nmod helpers;\n'
                "pub mod other;\npub mod user;\n"
            ),
            "src/helpers.rs": "pub trait Base {\n    fn go(&self) -> i32;\n}\n",
            "src/other.rs": "pub trait Base {\n    fn go(&self) -> i32;\n}\n",
            "src/user.rs": (
                "pub struct S;\n"
                "impl crate::helpers::Base for S {\n"
                "    fn go(&self) -> i32 {\n        1\n    }\n"
                "}\n"
            ),
        },
    )

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    edges = _implements(mock_ingestor)
    assert not [pair for pair in edges if pair[1].endswith("Base")], edges
