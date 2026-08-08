"""Rust declaration reads must see exactly what the indexer indexes.

The `#[path]` redirect sweep walks with the indexer's predicate (issue #1088),
but the per-file declaration reads behind it read candidate declaring files
straight from disk (issue #1100). A `mod` declaration in a file the user
excluded then decides where an indexed module sits: the target keeps its
physical `super::` climb into a tree the graph does not hold, instead of
following the redirect that IS indexed.
"""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.tests.test_rust_crate_path_trait_linking import (
    _calls,
    _write,
    create_and_run_updater,
)

CARGO = '[package]\nname = "{name}"\nversion = "0.1.0"\n'


def _redirect_corpus(name: str) -> dict[str, str]:
    # src/fixtures/mod.rs declares `rig` where the file physically sits, so
    # rustc compiles it into that tree and the redirect in tools/gen.rs is
    # suppressed. Excluding mod.rs removes that tree from the graph entirely.
    return {
        "Cargo.toml": CARGO.format(name=name),
        "src/lib.rs": "pub mod fixtures;\n",
        "src/fixtures/mod.rs": "pub mod rig;\npub mod sib;\n",
        "src/fixtures/sib.rs": "pub fn run() -> i32 {\n    1\n}\n",
        "src/fixtures/rig.rs": "pub fn build() -> i32 {\n    super::sib::run()\n}\n",
        "tools/gen.rs": (
            '#[path = "../src/fixtures/rig.rs"]\nmod rig;\nmod sib;\nfn main() {}\n'
        ),
        "tools/gen/sib.rs": "pub fn run() -> i32 {\n    9\n}\n",
    }


def test_an_indexed_declaring_neighbour_still_suppresses_the_redirect(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # Control: with every file indexed, the physical declaration wins and
    # `super::` counts from src.fixtures, exactly as rustc does.
    name = "rs_decl_neighbour_indexed"
    project = temp_repo / name
    _write(project, _redirect_corpus(name))

    create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    calls = _calls(mock_ingestor)
    caller = f"{name}.src.fixtures.rig.build"
    assert (caller, f"{name}.src.fixtures.sib.run") in calls, calls


def test_an_excluded_declaring_neighbour_no_longer_suppresses_the_redirect(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/fixtures/mod.rs is excluded, so the graph holds no tree in which
    # rig is declared where it sits. The only declaration the graph does hold
    # is the redirect in tools/gen.rs, so `super::` must count from there.
    name = "rs_decl_neighbour_excluded"
    project = temp_repo / name
    _write(project, _redirect_corpus(name))

    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"src/fixtures/mod.rs"}),
    )

    processor = updater.factory.import_processor
    assert not processor._rust_module_is_declared(["src", "fixtures", "rig"])

    # The Module node stays keyed by the file's own path; it is the `super::`
    # climb inside it that follows the redirect instead of the physical tree.
    calls = _calls(mock_ingestor)
    caller = f"{name}.src.fixtures.rig.build"
    assert (caller, f"{name}.tools.gen.sib.run") in calls, calls
    assert (caller, f"{name}.src.fixtures.sib.run") not in calls, calls


def test_excluded_entry_file_declarations_do_not_shape_resolution(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # An entry file's `mod` declarations feed crate path and gate resolution.
    # One the indexer skipped must contribute nothing to that map.
    name = "rs_decl_entry_excluded"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod alpha;\n",
            "src/main.rs": "mod beta;\nfn main() {}\n",
            "src/alpha.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/beta.rs": "pub fn run() -> i32 {\n    2\n}\n",
        },
    )

    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"src/main.rs"}),
    )

    decls = updater.factory.import_processor._rust_entry_decls(["src"])
    assert "main" not in decls, decls
    assert "lib" in decls, decls


def test_unexcluded_entry_files_still_contribute_declarations(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    name = "rs_decl_entry_indexed"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod alpha;\n",
            "src/main.rs": "mod beta;\nfn main() {}\n",
            "src/alpha.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/beta.rs": "pub fn run() -> i32 {\n    2\n}\n",
        },
    )

    updater = create_and_run_updater(project, mock_ingestor, skip_if_missing="rust")

    decls = updater.factory.import_processor._rust_entry_decls(["src"])
    assert {"lib", "main"} <= set(decls), decls
    assert "beta" in decls["main"].mods, decls


def test_an_excluded_sibling_file_falls_through_to_mod_rs(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # src/engine.rs and src/engine/mod.rs both back src.engine. Declaring
    # both is a rustc error, but excluding one leaves the other as the only
    # file the graph holds, so it must be the one whose declarations count.
    name = "rs_decl_sibling_excluded"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod engine;\n",
            "src/engine.rs": "pub mod thing;\n",
            "src/engine/mod.rs": "pub mod thing;\npub mod extra;\n",
            "src/engine/thing.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/engine/extra.rs": "pub fn run() -> i32 {\n    2\n}\n",
        },
    )

    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"src/engine.rs"}),
    )

    found = updater.factory.import_processor._rust_module_decls(
        ["src", "engine"], want_mods=True
    )
    assert found is not None
    assert "extra" in found[0].mods, found


def test_watch_refresh_does_not_cache_an_excluded_entry_file(
    temp_repo: Path, mock_ingestor: MagicMock
) -> None:
    # The watcher's relevance filter only knows the built-in ignores, so an
    # excluded entry file still reaches refresh_rust_path_caches_for. If its
    # declarations land in the cache, _rust_entry_decls returns them before
    # its own exclude gate ever runs.
    name = "rs_watch_excluded_entry"
    project = temp_repo / name
    _write(
        project,
        {
            "Cargo.toml": CARGO.format(name=name),
            "src/lib.rs": "pub mod alpha;\n",
            "src/main.rs": "mod beta;\nfn main() {}\n",
            "src/alpha.rs": "pub fn run() -> i32 {\n    1\n}\n",
            "src/beta.rs": "pub fn run() -> i32 {\n    2\n}\n",
        },
    )
    updater = create_and_run_updater(
        project,
        mock_ingestor,
        skip_if_missing="rust",
        exclude_paths=frozenset({"src/main.rs"}),
    )
    processor = updater.factory.import_processor

    # Establish the precondition explicitly: the refresh only writes when the
    # entry-declaration cache already holds this directory, so without this
    # the assertion below could hold for the wrong reason.
    primed = processor._rust_entry_decls(["src"])
    assert "lib" in primed, primed
    assert "main" not in primed, primed

    processor.refresh_rust_path_caches_for(project / "src" / "main.rs", created=False)

    decls = processor._rust_entry_decls(["src"])
    assert "main" not in decls, decls
