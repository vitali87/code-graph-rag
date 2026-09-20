"""Cargo's per-kind discovery opt-outs (autobins/autoexamples/autotests/
autobenches/autolib and build=false) must stop the corresponding auto
locations from classifying as automatic crate roots, while an explicit
manifest target at the same path still roots the file (issue #1030)."""

from pathlib import Path

import pytest

from codebase_rag.parsers.import_processor import ImportProcessor


def _processor(repo: Path) -> ImportProcessor:
    return ImportProcessor(
        repo_path=repo,
        project_name="proj",
        ingestor=None,
        function_registry=None,
    )


def _package(repo: Path, manifest_extra: str = "") -> None:
    (repo / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\n' + manifest_extra
    )
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "lib.rs").write_text("pub fn seed() {}\n")


@pytest.mark.parametrize(
    ("key", "dir_name"),
    [
        ("autotests", "tests"),
        ("autoexamples", "examples"),
        ("autobenches", "benches"),
    ],
)
def test_disabled_kind_stops_auto_classification(
    tmp_path: Path, key: str, dir_name: str
) -> None:
    _package(tmp_path, f"{key} = false\n")
    (tmp_path / dir_name).mkdir()
    (tmp_path / dir_name / "probe.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir([dir_name], "probe") is False


@pytest.mark.parametrize(
    ("key", "dir_name"),
    [
        ("autotests", "tests"),
        ("autoexamples", "examples"),
        ("autobenches", "benches"),
    ],
)
def test_enabled_kind_keeps_auto_classification(
    tmp_path: Path, key: str, dir_name: str
) -> None:
    _package(tmp_path)
    (tmp_path / dir_name).mkdir()
    (tmp_path / dir_name / "probe.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir([dir_name], "probe") is True


def test_disabled_kind_still_roots_an_explicit_target(tmp_path: Path) -> None:
    _package(
        tmp_path,
        'autotests = false\n\n[[test]]\nname = "probe"\npath = "tests/probe.rs"\n',
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "probe.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir(["tests"], "probe") is False
    assert processor._rust_is_explicit_target(["tests"], "probe") is True


def test_autobins_false_stops_src_bin_classification(tmp_path: Path) -> None:
    _package(tmp_path, "autobins = false\n")
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "tool.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir(["src", "bin"], "tool") is False


def test_autobins_default_keeps_src_bin_classification(tmp_path: Path) -> None:
    _package(tmp_path)
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "tool.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir(["src", "bin"], "tool") is True


def test_build_false_stops_build_script_classification(tmp_path: Path) -> None:
    _package(tmp_path, "build = false\n")
    (tmp_path / "build.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir([], "build") is False


def test_build_default_keeps_build_script_classification(tmp_path: Path) -> None:
    _package(tmp_path)
    (tmp_path / "build.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_auto_target_dir([], "build") is True


def test_autolib_false_stops_crate_root_dir_classification(tmp_path: Path) -> None:
    _package(tmp_path, "autolib = false\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src"]) is False


def test_autolib_false_with_explicit_lib_target_still_roots(tmp_path: Path) -> None:
    _package(tmp_path, 'autolib = false\n\n[lib]\npath = "src/lib.rs"\n')
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src"]) is True


def test_autobins_false_alone_stops_main_rooting_the_dir(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\nautobins = false\n'
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src"]) is False


def test_default_flags_keep_crate_root_dir_classification(tmp_path: Path) -> None:
    _package(tmp_path)
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src"]) is True


def test_pathless_lib_table_survives_autolib_false(tmp_path: Path) -> None:
    """[lib] with no path is an explicit target at the default src/lib.rs."""
    _package(tmp_path, 'autolib = false\n\n[lib]\nname = "fixture"\n')
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src"]) is True
    assert "lib" in processor._rust_entry_decls(["src"])


def test_pathless_bin_table_survives_autobins_false(tmp_path: Path) -> None:
    _package(tmp_path, 'autobins = false\n\n[[bin]]\nname = "tool"\n')
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "tool.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_explicit_target(["src", "bin"], "tool") is True


def test_autolib_false_member_is_not_an_importable_lib(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["member"]\n')
    member = tmp_path / "member"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text(
        '[package]\nname = "member"\nversion = "0.1.0"\nautolib = false\n'
    )
    (member / "src" / "lib.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    manifest = processor._rust_read_manifest(member)
    assert processor._rust_member_lib_root(member, manifest) is None


def test_pathless_lib_member_stays_importable_under_autolib_false(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text('[workspace]\nmembers = ["member"]\n')
    member = tmp_path / "member"
    (member / "src").mkdir(parents=True)
    (member / "Cargo.toml").write_text(
        '[package]\nname = "member"\nversion = "0.1.0"\nautolib = false\n\n'
        '[lib]\nname = "member"\n'
    )
    (member / "src" / "lib.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    manifest = processor._rust_read_manifest(member)
    assert processor._rust_member_lib_root(member, manifest) == (
        ("member", "src"),
        "lib",
    )


def test_autobins_false_stops_multi_file_bin_dir(tmp_path: Path) -> None:
    """src/bin/<name>/main.rs is the bin kind's auto target too."""
    _package(tmp_path, "autobins = false\n")
    tool = tmp_path / "src" / "bin" / "tool"
    tool.mkdir(parents=True)
    (tool / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src", "bin", "tool"]) is False


def test_autotests_false_stops_multi_file_test_dir(tmp_path: Path) -> None:
    _package(tmp_path, "autotests = false\n")
    suite = tmp_path / "tests" / "suite"
    suite.mkdir(parents=True)
    (suite / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["tests", "suite"]) is False


def test_default_flags_keep_multi_file_target_dirs(tmp_path: Path) -> None:
    _package(tmp_path)
    tool = tmp_path / "src" / "bin" / "tool"
    tool.mkdir(parents=True)
    (tool / "main.rs").write_text("fn main() {}\n")
    suite = tmp_path / "tests" / "suite"
    suite.mkdir(parents=True)
    (suite / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src", "bin", "tool"]) is True
    assert processor._rust_is_crate_root_dir(["tests", "suite"]) is True


def test_pathless_package_name_bin_resolves_to_src_main(tmp_path: Path) -> None:
    """[[bin]] name = <package name> is src/main.rs, kept under autobins=false."""
    _package(tmp_path, 'autobins = false\n\n[[bin]]\nname = "fixture"\n')
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_explicit_target(["src"], "main") is True
    assert processor._rust_is_crate_root_dir(["src"]) is True


def test_pathless_bin_resolves_to_multi_file_target(tmp_path: Path) -> None:
    _package(tmp_path, 'autobins = false\n\n[[bin]]\nname = "tool"\n')
    tool = tmp_path / "src" / "bin" / "tool"
    tool.mkdir(parents=True)
    (tool / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_explicit_target(["src", "bin", "tool"], "main") is True


def test_ambiguous_pathless_bin_resolves_nowhere(tmp_path: Path) -> None:
    """Cargo errors on tool.rs + tool/main.rs both existing; record neither."""
    _package(tmp_path, 'autobins = false\n\n[[bin]]\nname = "tool"\n')
    (tmp_path / "src" / "bin" / "tool").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "tool.rs").write_text("fn main() {}\n")
    (tmp_path / "src" / "bin" / "tool" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_explicit_target(["src", "bin"], "tool") is False
    assert processor._rust_is_explicit_target(["src", "bin", "tool"], "main") is False


def test_autobins_false_stops_direct_src_bin_main(tmp_path: Path) -> None:
    """src/bin/main.rs is itself a bin auto target."""
    _package(tmp_path, "autobins = false\n")
    (tmp_path / "src" / "bin").mkdir(parents=True)
    (tmp_path / "src" / "bin" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src", "bin"]) is False


def test_autoexamples_false_stops_direct_examples_main(tmp_path: Path) -> None:
    _package(tmp_path, "autoexamples = false\n")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["examples"]) is False


def test_autoexamples_false_stops_direct_examples_lib(tmp_path: Path) -> None:
    """A lib.rs directly in a kind dir is that kind's target, not a library."""
    _package(tmp_path, "autoexamples = false\n")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "lib.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["examples"]) is False


def test_default_direct_examples_main_still_roots(tmp_path: Path) -> None:
    _package(tmp_path)
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["examples"]) is True


def test_nested_lib_rs_is_never_a_target(tmp_path: Path) -> None:
    """Cargo multi-file targets compile <name>/main.rs; a lone lib.rs in a
    nested target dir is not a crate root even with default flags."""
    _package(tmp_path)
    tool = tmp_path / "src" / "bin" / "tool"
    tool.mkdir(parents=True)
    (tool / "lib.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    assert processor._rust_is_crate_root_dir(["src", "bin", "tool"]) is False


def test_autolib_false_drops_lib_from_the_entry_scan(tmp_path: Path) -> None:
    _package(tmp_path, "autolib = false\n")
    (tmp_path / "src" / "lib.rs").write_text("pub mod foo;\n")
    (tmp_path / "src" / "foo.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    assert "lib" not in processor._rust_entry_decls(["src"])


def test_autolib_default_keeps_lib_in_the_entry_scan(tmp_path: Path) -> None:
    _package(tmp_path)
    (tmp_path / "src" / "lib.rs").write_text("pub mod foo;\n")
    (tmp_path / "src" / "foo.rs").write_text("pub fn f() {}\n")
    processor = _processor(tmp_path)
    assert "lib" in processor._rust_entry_decls(["src"])


def test_manifest_refresh_evicts_newly_disabled_entry_stems(tmp_path: Path) -> None:
    """A watched manifest edit flipping autolib/autobins to false must leave
    the entry-declaration map exactly as a clean index would."""
    _package(tmp_path)
    (tmp_path / "src" / "main.rs").write_text("fn main() {}\n")
    processor = _processor(tmp_path)
    warmed = processor._rust_entry_decls(["src"])
    assert "lib" in warmed
    assert "main" in warmed

    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\nversion = "0.1.0"\n'
        "autolib = false\nautobins = false\n"
    )
    processor.refresh_rust_path_caches_for(tmp_path / "Cargo.toml", created=False)
    refreshed = processor._rust_entry_decls(["src"])
    assert "lib" not in refreshed
    assert "main" not in refreshed
    fresh = _processor(tmp_path)._rust_entry_decls(["src"])
    assert set(refreshed) == set(fresh)
