"""Which Go files one build compiles together (issue #2483, PR #3360)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.parsers.go.builds import (
    ALWAYS,
    GoBuilds,
    header_constraint,
    name_constraint,
    satisfiable,
)


@pytest.mark.parametrize(
    ("first", "second", "together"),
    [
        ("i_linux.go", "i_windows.go", False),
        ("i_linux.go", "i_amd64.go", True),
        ("i_linux_amd64.go", "i_linux_arm64.go", False),
        ("i_windows_amd64.go", "i_amd64.go", True),
        ("i_linux_test.go", "i_windows.go", False),
        # GOOS=android builds `_linux` files too.
        ("i_android.go", "i_linux.go", True),
        # Only a trailing GOOS or GOARCH counts, and not without a `_`.
        ("linux.go", "windows.go", True),
        ("i_linux_extra.go", "i_windows.go", True),
    ],
)
def test_a_file_name_constrains_its_build(
    first: str, second: str, together: bool
) -> None:
    assert satisfiable((name_constraint(first), name_constraint(second))) is together


@pytest.mark.parametrize(
    ("first", "second", "together"),
    [
        ("//go:build linux\n", "//go:build !linux\n", False),
        ("//go:build linux || darwin\n", "//go:build windows\n", False),
        ("//go:build unix\n", "//go:build windows\n", False),
        ("//go:build unix\n", "//go:build darwin\n", True),
        ("//go:build linux && !(arm64 || cgo)\n", "//go:build arm64\n", False),
        # A tag no GOOS or GOARCH fixes may be either.
        ("//go:build foo\n", "//go:build !foo\n", False),
        ("//go:build foo\n", "//go:build bar\n", True),
        ("//go:build go1.21\n", "//go:build !go1.22\n", True),
        # The legacy form, used only without a //go:build line.
        ("// +build linux,amd64 darwin\n\n", "// +build windows\n\n", False),
        ("// +build linux\n\n", "//go:build linux\n", True),
    ],
)
def test_a_build_line_constrains_its_build(
    first: str, second: str, together: bool
) -> None:
    assert (
        satisfiable((header_constraint(first), header_constraint(second))) is together
    )


@pytest.mark.parametrize(
    "header",
    [
        # Not a constraint: no blank line ends the legacy block, the line
        # follows the package clause, or it does not parse.
        "// +build linux\npackage p\n",
        "package p\n\n//go:build linux\n",
        "//go:build linux &&\n\npackage p\n",
        "//go:buildlinux\n\npackage p\n",
    ],
)
def test_what_is_no_constraint_holds_in_every_build(header: str) -> None:
    assert header_constraint(header) == ALWAYS


def test_variants_are_the_declarations_no_build_compiles_together(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.go").write_text("//go:build linux\n\npackage p\n", encoding="utf-8")
    (tmp_path / "b.go").write_text("//go:build !linux\n\npackage p\n", encoding="utf-8")
    (tmp_path / "c.go").write_text("package p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    found = builds.variants([("p.b.I", "b.go"), ("p.a.I", "a.go")], "c.go")
    assert found == ("p.a.I", "p.b.I")
    assert builds.variants([("p.a.I", "a.go")], "x_windows.go") == ()
    assert builds.variants([("p.a.I", "a.go"), ("p.c.I", "c.go")], "c.go") is None
    assert builds.covered("c.go", ["a.go", "b.go"])
    assert not builds.covered("c.go", ["a.go"])


def test_a_changed_file_is_read_again(tmp_path: Path) -> None:
    path = tmp_path / "a.go"
    path.write_text("//go:build linux\n\npackage p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    assert not builds.together(("a.go", "x_windows.go"))
    path.write_text("//go:build windows\n\npackage p\n\n// grown\n", encoding="utf-8")
    assert builds.together(("a.go", "x_windows.go"))


@pytest.mark.parametrize(
    "header",
    [
        "//go:build " + "(" * 300 + "linux" + ")" * 300 + "\n",
        "//go:build " + "!" * 5000 + "linux\n",
        "//go:build " + " && ".join(["linux"] * 900) + "\n",
        "//go:build ((linux)\n",
        "//go:build linux))\n",
        "//go:build ) linux (\n",
        "//go:build linux && || windows\n",
        "//go:build linux $ windows\n",
        "//go:build \x00\n",
        "// +build ,,, !\n\n",
    ],
)
def test_a_constraint_this_cannot_read_holds_in_every_build(
    tmp_path: Path, header: str
) -> None:
    # Malformed, or nested past what this reads: never an exception, and
    # the file builds with anything.
    (tmp_path / "a.go").write_text(f"{header}\npackage p\n", encoding="utf-8")
    builds = GoBuilds(tmp_path)
    assert builds.together(("a.go", "b_windows.go"))
    assert builds.together(("a.go", "b_linux.go"))


def test_a_constraint_nested_within_the_limit_is_read() -> None:
    nested = "//go:build " + "(" * 50 + "linux" + ")" * 50 + "\n"
    assert not satisfiable((header_constraint(nested), name_constraint("x_windows.go")))
