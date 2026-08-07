"""The `#[path]` redirect sweep must see exactly what the indexer indexes.

The sweep walks the repository for redirect declarations and prunes with
the built-in ignore set alone, while the indexer also honours `--exclude`
and `.cgrignore` rescues (issue #1088). A declaration inside an excluded
subtree then claims the qn of an indexed file, sending `super::` in that
file towards a module the user asked cgr not to index; a declaration in a
rescued subtree is missed, leaving `super::` on the physical climb where a
shadow sibling answers.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)


def test_a_redirect_declared_in_an_excluded_subtree_claims_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `--exclude tools/` removes the whole subtree from the index, so the
    # declaration in tools/gen.rs must not move src/fixtures/rig.rs: with
    # no declarer in the graph, `super::` in it keeps the physical climb.
    # The map is asserted directly because every module the poisoned entry
    # can answer with is unindexed, so the resolver's fallback happens to
    # land the right edge today; the poison is the map entry itself.
    project = temp_repo / "rs_sweep_excluded_dir"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_sweep_excluded_dir"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub fn top() -> i32 {\n    0\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
            "tools/gen.rs": (
                '#[path = "../src/fixtures/rig.rs"]\nmod rig;\nmod sib;\nfn main() {}\n'
            ),
            "tools/gen/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
        },
    )
    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"tools/"}),
    )
    parents = updater.factory.import_processor._rust_redirect_parent_map()
    assert parents == {}, parents
    calls = _calls(mock_ingestor)
    caller = "rs_sweep_excluded_dir.src.fixtures.rig.build"
    assert (caller, "rs_sweep_excluded_dir.src.fixtures.sib.run") in calls, calls
    assert not [pair for pair in calls if ".tools." in pair[1]], calls


def test_a_redirect_excluded_by_a_file_pattern_claims_nothing(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The exclusion names one FILE, so the directory walk keeps tools/ and
    # only the per-file filter can drop the declaration. Here the decoy
    # module IS indexed, so a sweep that reads the excluded file records a
    # live wrong edge rather than a dangling one.
    project = temp_repo / "rs_sweep_excluded_file"
    _write(
        project,
        {
            "Cargo.toml": (
                '[package]\nname = "rs_sweep_excluded_file"\nversion = "0.1.0"\n'
            ),
            "src/lib.rs": "pub fn top() -> i32 {\n    0\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/fixtures/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
            "tools/gen.rs": (
                '#[path = "../src/fixtures/rig.rs"]\nmod rig;\nmod sib;\nfn main() {}\n'
            ),
            "tools/gen/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
        },
    )
    create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"tools/gen.rs"}),
    )
    calls = _calls(mock_ingestor)
    caller = "rs_sweep_excluded_file.src.fixtures.rig.build"
    assert (caller, "rs_sweep_excluded_file.src.fixtures.sib.run") in calls, calls
    assert (caller, "rs_sweep_excluded_file.tools.gen.sib.run") not in calls, calls


def test_a_redirect_rescued_by_unignore_moves_its_target(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # `build` is a built-in ignore rescued by a `.cgrignore` unignore, so
    # the indexer walks it and the graph holds build/gen.rs. The sweep has
    # to walk there too, or the redirect is missed and `super::` in the
    # target resolves to the physical shadow beside the file.
    project = temp_repo / "rs_sweep_rescued"
    _write(
        project,
        {
            "Cargo.toml": ('[package]\nname = "rs_sweep_rescued"\nversion = "0.1.0"\n'),
            "src/lib.rs": "pub fn top() -> i32 {\n    0\n}\n",
            "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
            "src/fixtures/rig.rs": (
                "pub fn build() -> i32 {\n    super::sib::run()\n}\n"
            ),
            "build/gen.rs": (
                '#[path = "../src/fixtures/rig.rs"]\nmod rig;\nmod sib;\n'
            ),
            "build/gen/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
        },
    )
    create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        unignore_paths=frozenset({"build/"}),
    )
    calls = _calls(mock_ingestor)
    caller = "rs_sweep_rescued.src.fixtures.rig.build"
    assert (caller, "rs_sweep_rescued.build.gen.sib.run") in calls, calls
    assert (caller, "rs_sweep_rescued.src.fixtures.sib.run") not in calls, calls
