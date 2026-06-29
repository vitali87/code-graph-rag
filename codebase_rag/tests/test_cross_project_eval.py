from pathlib import Path

from evals import constants as ec
from evals.cross_project import cgr_cross_package, score_cross_project


def _make_monorepo(root: Path) -> None:
    # (H) Two sibling top-level packages plus a third; pkg_b and pkg_c both reach
    # (H) into pkg_a, which no single-top-level-package corpus exercises.
    for pkg in ("pkg_a", "pkg_b", "pkg_c"):
        (root / pkg).mkdir(parents=True)
        (root / pkg / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg_a" / "core.py").write_text(
        "def shared():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg_b" / "use.py").write_text(
        "from pkg_a.core import shared\n\n\ndef run():\n    return shared()\n",
        encoding="utf-8",
    )
    (root / "pkg_c" / "only_import.py").write_text(
        "from pkg_a import core\n", encoding="utf-8"
    )


def test_cgr_resolves_cross_package_calls_and_imports(tmp_path: Path) -> None:
    _make_monorepo(tmp_path)
    calls, imports = cgr_cross_package(tmp_path, tmp_path.name)
    project = tmp_path.name

    # (H) pkg_b.use.run() calls pkg_a.core.shared() across the package boundary.
    assert (f"{project}.pkg_b.use.run", f"{project}.pkg_a.core.shared") in calls
    # (H) both pkg_b and pkg_c import a pkg_a module.
    assert (f"{project}.pkg_b.use", f"{project}.pkg_a.core") in imports
    assert any(src == f"{project}.pkg_c.only_import" for src, _t in imports)


def test_intra_package_edges_are_not_cross_package(tmp_path: Path) -> None:
    root = tmp_path / "mono"
    (root / "pkg_a").mkdir(parents=True)
    (root / "pkg_a" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg_a" / "a.py").write_text(
        "def helper():\n    return 1\n", encoding="utf-8"
    )
    (root / "pkg_a" / "b.py").write_text(
        "from pkg_a.a import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    calls, imports = cgr_cross_package(root, "mono")
    # (H) Everything is within pkg_a, so there are no cross-package edges.
    assert calls == set()
    assert imports == set()


def test_score_cross_project_prf() -> None:
    cgr = ({("a", "b")}, {("m", "n")})
    oracle = ({("a", "b")}, {("m", "x")})
    result = score_cross_project(cgr, oracle)
    calls_row = next(r for r in result.rows if r["label"] == ec.CROSS_CALLS_LABEL)
    imports_row = next(r for r in result.rows if r["label"] == ec.CROSS_IMPORTS_LABEL)
    assert (calls_row["tp"], calls_row["fp"], calls_row["fn"]) == (1, 0, 0)
    assert (imports_row["tp"], imports_row["fp"], imports_row["fn"]) == (0, 1, 1)
