"""Direct pins for the two source-root helpers, one branch per test.

`test_python_source_root_imports.py` drives the same code end to end through
the import processor. These tests call the helpers directly so that every
branch has a test naming it, which is what lets the functions be split into
smaller pieces (#1669) without a behaviour change hiding in the seams.
"""

from pathlib import Path

from codebase_rag.parsers.python_source_roots import (
    _package_dir_remaps,
    resolve_via_source_roots,
)

PYPROJECT = "[tool.setuptools.package-dir]\n"


def _pyproject(repo: Path, body: str, where: str = "") -> Path:
    base = repo / where if where else repo
    base.mkdir(parents=True, exist_ok=True)
    path = base / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


# --- _package_dir_remaps ----------------------------------------------------


def test_malformed_pyproject_yields_no_remaps(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, "[tool.setuptools\n")
    assert _package_dir_remaps(path, tmp_path) == []


def test_non_table_tool_section_yields_no_remaps(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    path = _pyproject(tmp_path, 'tool = "not a table"\n')
    assert _package_dir_remaps(path, tmp_path) == []


def test_non_table_package_dir_yields_no_remaps(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    path = _pyproject(tmp_path, '[tool.setuptools]\npackage-dir = "lib"\n')
    assert _package_dir_remaps(path, tmp_path) == []


def test_missing_setuptools_table_yields_no_remaps(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, '[project]\nname = "proj"\n')
    assert _package_dir_remaps(path, tmp_path) == []


def test_non_string_remap_value_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "lib").mkdir()
    # `3` names a real directory, so only the isinstance guard can reject the
    # non-string value; without it, str(3) would resolve and be kept.
    (tmp_path / "3").mkdir()
    path = _pyproject(tmp_path, PYPROJECT + 'bad = 3\ngood = "lib"\n')
    assert _package_dir_remaps(path, tmp_path) == [("good", "lib")]


def test_remap_to_a_missing_directory_is_skipped(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, PYPROJECT + 'mypkg = "nowhere"\n')
    assert _package_dir_remaps(path, tmp_path) == []


def test_remap_to_a_file_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "lib.py").write_text("", encoding="utf-8")
    path = _pyproject(tmp_path, PYPROJECT + 'mypkg = "lib.py"\n')
    assert _package_dir_remaps(path, tmp_path) == []


def test_remap_escaping_the_repo_is_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (tmp_path / "outside").mkdir()
    path = _pyproject(repo, PYPROJECT + 'mypkg = "../outside"\n')
    assert _package_dir_remaps(path, repo) == []


def test_named_remap_maps_the_name_to_the_repo_relative_dotted_dir(
    tmp_path: Path,
) -> None:
    (tmp_path / "packages/a/lib").mkdir(parents=True)
    path = _pyproject(tmp_path, PYPROJECT + 'mypkg = "lib"\n', where="packages/a")
    assert _package_dir_remaps(path, tmp_path) == [("mypkg", "packages.a.lib")]


def test_dotted_remap_name_is_kept_whole(tmp_path: Path) -> None:
    (tmp_path / "lib/widgets").mkdir(parents=True)
    path = _pyproject(tmp_path, PYPROJECT + '"acme.widgets" = "lib/widgets"\n')
    assert _package_dir_remaps(path, tmp_path) == [("acme.widgets", "lib.widgets")]


def test_default_remap_maps_each_child_directory_and_module(
    tmp_path: Path,
) -> None:
    lib = tmp_path / "lib"
    (lib / "zeta").mkdir(parents=True)
    (lib / "alpha").mkdir()
    (lib / "mod.py").write_text("", encoding="utf-8")
    (lib / "__init__.py").write_text("", encoding="utf-8")
    (lib / "notes.txt").write_text("", encoding="utf-8")
    path = _pyproject(tmp_path, PYPROJECT + '"" = "lib"\n')
    # sorted by child name; __init__.py and non-Python files contribute nothing
    assert _package_dir_remaps(path, tmp_path) == [
        ("alpha", "lib.alpha"),
        ("mod", "lib.mod"),
        ("zeta", "lib.zeta"),
    ]


def test_named_and_default_remaps_combine_in_declaration_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "lib/child").mkdir(parents=True)
    (tmp_path / "other").mkdir()
    path = _pyproject(tmp_path, PYPROJECT + '"" = "lib"\nmypkg = "other"\n')
    assert _package_dir_remaps(path, tmp_path) == [
        ("child", "lib.child"),
        ("mypkg", "other"),
    ]


# --- resolve_via_source_roots -----------------------------------------------


def test_unknown_top_level_name_resolves_to_nothing(tmp_path: Path) -> None:
    assert resolve_via_source_roots(tmp_path, {}, "pkg.impls") is None


def test_prefix_must_match_on_a_dot_boundary(tmp_path: Path) -> None:
    roots = {"acme": [("acme.widgets", "lib.widgets")]}
    assert resolve_via_source_roots(tmp_path, roots, "acme.widgetsx") is None


def test_exact_match_on_a_package_directory(tmp_path: Path) -> None:
    (tmp_path / "packages/a/src/pkg").mkdir(parents=True)
    roots = {"pkg": [("pkg", "packages.a.src.pkg")]}
    assert resolve_via_source_roots(tmp_path, roots, "pkg") == "packages.a.src.pkg"


def test_exact_match_on_a_single_module_file(tmp_path: Path) -> None:
    (tmp_path / "packages/a/src").mkdir(parents=True)
    (tmp_path / "packages/a/src/mymod.py").write_text("", encoding="utf-8")
    roots = {"mymod": [("mymod", "packages.a.src.mymod")]}
    assert resolve_via_source_roots(tmp_path, roots, "mymod") == "packages.a.src.mymod"


def test_submodule_confirmed_by_a_file_on_disk(tmp_path: Path) -> None:
    (tmp_path / "packages/a/src/pkg").mkdir(parents=True)
    (tmp_path / "packages/a/src/pkg/impls.py").write_text("", encoding="utf-8")
    roots = {"pkg": [("pkg", "packages.a.src.pkg")]}
    assert (
        resolve_via_source_roots(tmp_path, roots, "pkg.impls")
        == "packages.a.src.pkg.impls"
    )


def test_subpackage_confirmed_by_a_directory_on_disk(tmp_path: Path) -> None:
    (tmp_path / "packages/a/src/pkg/sub").mkdir(parents=True)
    roots = {"pkg": [("pkg", "packages.a.src.pkg")]}
    assert (
        resolve_via_source_roots(tmp_path, roots, "pkg.sub") == "packages.a.src.pkg.sub"
    )


def test_the_root_that_holds_the_submodule_wins_regardless_of_order(
    tmp_path: Path,
) -> None:
    (tmp_path / "packages/a/src/common").mkdir(parents=True)
    (tmp_path / "packages/b/src/common").mkdir(parents=True)
    (tmp_path / "packages/b/src/common/only_in_b.py").write_text("", encoding="utf-8")
    roots = {
        "common": [
            ("common", "packages.a.src.common"),
            ("common", "packages.b.src.common"),
        ]
    }
    assert (
        resolve_via_source_roots(tmp_path, roots, "common.only_in_b")
        == "packages.b.src.common.only_in_b"
    )


def test_a_sole_match_is_trusted_without_disk_confirmation(tmp_path: Path) -> None:
    roots = {"pkg": [("pkg", "packages.a.src.pkg")]}
    assert (
        resolve_via_source_roots(tmp_path, roots, "pkg.missing")
        == "packages.a.src.pkg.missing"
    )
    assert resolve_via_source_roots(tmp_path, roots, "pkg") == "packages.a.src.pkg"


def test_several_unconfirmed_matches_resolve_to_nothing(tmp_path: Path) -> None:
    roots = {
        "common": [
            ("common", "packages.a.src.common"),
            ("common", "packages.b.src.common"),
        ]
    }
    assert resolve_via_source_roots(tmp_path, roots, "common.missing") is None


def test_the_longest_dotted_prefix_is_tried_first(tmp_path: Path) -> None:
    # Both roots answer for `acme.widgets.impl`, and BOTH are confirmed on disk,
    # so only the ordering decides. The bare `acme` root would answer
    # `lib.widgets.impl` via lib/widgets/impl.py; the dotted `acme.widgets`
    # remap answers `other.impl` via other/impl.py. The more specific remap
    # is tried first, so the answer is the second.
    (tmp_path / "lib/widgets").mkdir(parents=True)
    (tmp_path / "lib/widgets/impl.py").write_text("", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other/impl.py").write_text("", encoding="utf-8")
    roots = {"acme": [("acme", "lib"), ("acme.widgets", "other")]}
    assert (
        resolve_via_source_roots(tmp_path, roots, "acme.widgets.impl") == "other.impl"
    )
