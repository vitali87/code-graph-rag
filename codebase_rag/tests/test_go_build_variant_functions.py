# (H) Go package-level functions are visible package-wide, but cgr keys each file as
# (H) its own module (pkgdir.file.name). Two same-package functions with the same name
# (H) can only be mutually-exclusive build-tag variants (gin's `validate` under
# (H) `//go:build !nomsgpack` vs `//go:build nomsgpack`), since the compiler rejects
# (H) duplicate top-level identifiers otherwise. A bare call resolves to just one file's
# (H) copy, so the other build's copy looks dead unless every same-package same-name
# (H) sibling is also referenced.
from pathlib import Path

from evals.cgr_graph import _capture


def test_build_variant_sibling_function_is_called(tmp_path: Path) -> None:
    # (H) Two `validate` funcs in the same package (build variants) + a caller in a
    # (H) third file. The bare `validate(x)` call must reach BOTH copies, so neither
    # (H) build's variant is reported dead.
    (tmp_path / "a.go").write_text(
        "package p\nfunc validate(x int) int { return x }\n",
        encoding="utf-8",
    )
    (tmp_path / "b.go").write_text(
        "package p\nfunc validate(x int) int { return x + 1 }\n",
        encoding="utf-8",
    )
    (tmp_path / "c.go").write_text(
        "package p\nfunc caller() int { return validate(1) }\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }
    assert ("proj.c.caller", "proj.a.validate") in calls
    assert ("proj.c.caller", "proj.b.validate") in calls


def test_different_package_same_name_is_not_fanned_out(tmp_path: Path) -> None:
    # (H) Guard: a same-name function in a DIFFERENT package (subdirectory) is a
    # (H) distinct function, never a build variant, so the caller must NOT reach it.
    (tmp_path / "a.go").write_text(
        "package p\nfunc validate(x int) int { return x }\n"
        "func caller() int { return validate(1) }\n",
        encoding="utf-8",
    )
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "s.go").write_text(
        "package sub\nfunc validate(x int) int { return x }\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }
    assert ("proj.a.caller", "proj.a.validate") in calls
    assert ("proj.a.caller", "proj.sub.s.validate") not in calls


def test_external_test_package_sibling_is_not_fanned_out(tmp_path: Path) -> None:
    # (H) Guard: Go allows an external test package `package p_test` in the SAME
    # (H) directory as `package p` (both compile from the same dir but are distinct
    # (H) packages). Production code can never call a function defined in a `_test.go`
    # (H) file, so a same-directory `_test.go` sibling must NOT receive a fan-out edge
    # (H) -- otherwise a genuinely test-only dead function is masked as live.
    (tmp_path / "a.go").write_text(
        "package p\nfunc validate(x int) int { return x }\n"
        "func caller() int { return validate(1) }\n",
        encoding="utf-8",
    )
    (tmp_path / "a_test.go").write_text(
        "package p_test\nfunc validate(x int) int { return x }\n",
        encoding="utf-8",
    )
    ingestor = _capture(tmp_path, "proj")
    calls = {
        (str(f), str(t)) for _fl, f, rel, _tl, t in ingestor.rels if rel == "CALLS"
    }
    assert ("proj.a.caller", "proj.a.validate") in calls
    assert ("proj.a.caller", "proj.a_test.validate") not in calls
