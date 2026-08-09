"""Cargo compiles src/bin/mod.rs (and explicit mod.rs-path targets) as a
target named `mod` whose crate root is the file itself; its crate:: paths
must resolve to the directory qn the mod.rs spelling maps to, never the
phantom project root (issue #1031)."""

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.import_processor import ImportProcessor


def _processor(repo: Path) -> ImportProcessor:
    return ImportProcessor(
        repo_path=repo,
        project_name="proj",
        ingestor=None,
        function_registry=None,
    )


def _manifest(repo: Path, extra: str = "") -> None:
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "rs_bin_mod"\nversion = "0.1.0"\nedition = "2021"\n' + extra
    )


BIN_MOD_SOURCE = """
use crate::helper as h;

pub const fn helper() -> u32 { 7 }

fn main() { let _ = h(); }
"""


def test_src_bin_mod_rs_roots_its_own_crate(tmp_path: Path) -> None:
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "mod.rs").write_text(BIN_MOD_SOURCE)
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.bin") == ("file", ["src", "bin"])


def test_src_bin_mod_rs_crate_paths_resolve_to_the_directory_qn(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "mod.rs").write_text(BIN_MOD_SOURCE)
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    imports = updater.factory.import_processor.import_mapping.get("proj.src.bin", {})
    assert imports.get("h") == "proj.src.bin.helper", imports


def test_explicit_mod_rs_target_roots_its_own_crate(tmp_path: Path) -> None:
    _manifest(tmp_path, '\n[[bin]]\nname = "tool"\npath = "src/tool/mod.rs"\n')
    (tmp_path / "src" / "tool").mkdir(parents=True)
    (tmp_path / "src" / "tool" / "mod.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.tool") == ("file", ["src", "tool"])


def test_mod_rs_sibling_does_not_unroot_src_bin_main(tmp_path: Path) -> None:
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "src" / "bin" / "mod.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src", "bin"]) is True


def test_ordinary_module_directory_mod_rs_stays_a_module(tmp_path: Path) -> None:
    _manifest(tmp_path)
    (tmp_path / "src" / "foo").mkdir(parents=True)
    (tmp_path / "src" / "lib.rs").write_text("pub mod foo;\n")
    (tmp_path / "src" / "foo" / "mod.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.foo") == ("classic", ["src"])
    assert processor._rust_is_crate_root_dir(["src", "foo"]) is False
