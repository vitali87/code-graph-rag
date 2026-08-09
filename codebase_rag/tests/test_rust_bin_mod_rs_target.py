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
    assert processor._rust_crate_root("proj.src.bin") == ("dir_file", ["src", "bin"])


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
    assert processor._rust_crate_root("proj.src.tool") == ("dir_file", ["src", "tool"])


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


def test_mod_rs_root_redirects_read_mod_rs_not_a_dir_shadow(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """The root's declarations live in mod.rs: a #[path] redirect declared
    there must steer crate:: paths, which a dir-unaware walk (looking for a
    src/bin.rs entry) would never see."""
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "mod.rs").write_text(
        '#[path = "alt.rs"]\nmod sub;\nuse crate::sub::f as g;\nfn main() { g(); }\n'
    )
    (tmp_path / "src" / "bin" / "alt.rs").write_text("pub fn f() {}\n")
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
    assert imports.get("g") == "proj.src.bin.alt.f", imports


def test_self_paths_in_src_bin_mod_rs_resolve_to_the_directory_qn(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "src" / "bin" / "mod.rs").write_text(
        "use self::helper as sh;\n\npub const fn helper() -> u32 { 7 }\n\n"
        "fn main() { let _ = sh(); }\n"
    )
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
    assert imports.get("sh") == "proj.src.bin.helper", imports


def test_descendant_module_roots_at_the_mod_rs_target(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """A submodule declared from src/bin/mod.rs roots at the target's
    directory qn, so its crate:: paths stay inside the mod crate."""
    _manifest(tmp_path)
    child = tmp_path / "src" / "bin" / "child"
    child.mkdir(parents=True)
    (tmp_path / "src" / "bin" / "mod.rs").write_text(
        "mod child;\n\npub const fn helper() -> u32 { 7 }\n\nfn main() {}\n"
    )
    (child / "mod.rs").write_text(
        "use crate::helper as h;\npub fn use_it() -> u32 { h() }\n"
    )
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.bin.child") == (
        "dir_file",
        ["src", "bin"],
    )
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    imports = updater.factory.import_processor.import_mapping.get(
        "proj.src.bin.child", {}
    )
    assert imports.get("h") == "proj.src.bin.helper", imports


def test_root_level_explicit_mod_rs_target_roots_the_project_qn(
    tmp_path: Path,
) -> None:
    _manifest(tmp_path, '\n[[bin]]\nname = "tool"\npath = "mod.rs"\n')
    (tmp_path / "mod.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj") == ("dir_file", [])


def test_src_bin_main_keeps_its_own_crate_beside_mod_rs(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """With both siblings, main.rs's crate:: items nest under its own qn."""
    _manifest(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "mod.rs").write_text(
        "pub const fn helper() -> u32 { 7 }\nfn main() {}\n"
    )
    (tmp_path / "src" / "bin" / "main.rs").write_text(
        "use crate::own as o;\n\npub const fn own() -> u32 { 1 }\n\n"
        "fn main() { let _ = o(); }\n"
    )
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.bin.main") == (
        "file",
        ["src", "bin", "main"],
    )
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    imports = updater.factory.import_processor.import_mapping.get(
        "proj.src.bin.main", {}
    )
    assert imports.get("o") == "proj.src.bin.main.own", imports


def test_root_level_mod_rs_redirects_are_read(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """#[path] redirects declared in a root-level mod.rs target steer its
    crate:: paths."""
    _manifest(tmp_path, '\n[[bin]]\nname = "tool"\npath = "mod.rs"\n')
    (tmp_path / "mod.rs").write_text(
        '#[path = "alt.rs"]\nmod sub;\nuse crate::sub::f as g;\nfn main() { g(); }\n'
    )
    (tmp_path / "alt.rs").write_text("pub fn f() {}\n")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    imports = updater.factory.import_processor.import_mapping.get("proj", {})
    assert imports.get("g") == "proj.alt.f", imports


def test_explicit_main_target_keeps_its_crate_beside_explicit_mod_target(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    _manifest(
        tmp_path,
        '\n[[bin]]\nname = "tools"\npath = "src/tools/mod.rs"\n\n'
        '[[bin]]\nname = "tools-main"\npath = "src/tools/main.rs"\n',
    )
    tools = tmp_path / "src" / "tools"
    tools.mkdir(parents=True)
    (tools / "mod.rs").write_text("fn main() {}\n")
    (tools / "main.rs").write_text(
        "use crate::own as o;\n\npub const fn own() -> u32 { 1 }\n\n"
        "fn main() { let _ = o(); }\n"
    )
    processor = _processor(tmp_path)
    assert processor._rust_crate_root("proj.src.tools.main") == (
        "entry",
        ["src", "tools", "main"],
    )
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=tmp_path,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run()
    imports = updater.factory.import_processor.import_mapping.get(
        "proj.src.tools.main", {}
    )
    assert imports.get("o") == "proj.src.tools.main.own", imports
