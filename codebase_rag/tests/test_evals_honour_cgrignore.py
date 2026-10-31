"""Grading must cover the file set indexing covers, not a wider one.

No eval arm consulted `.cgrignore`, so a reported precision/recall was
measured over files the product would never put in a graph (issue #1520).
An excluded directory is usually vendored or generated code whose shape
differs from first-party source, so the error is not benign in either
direction.

Every test here carries a VISIBLE row as a control. An empty result cannot
distinguish "excluded correctly" from "the fixture produced nothing", and
the issue reports a first attempt that passed for exactly that wrong
reason: the fixture returned `[]` with and without `.cgrignore`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.inheritance import (
    cgr_inheritance,
    cpp_cgr_inheritance,
    cpp_oracle_inheritance,
    java_cgr_inheritance,
    java_oracle_inheritance,
    oracle_inheritance,
    score_inheritance,
)
from evals.oracles import cpp_available, java_available

_VISIBLE = "class Base2:\n    pass\n\n\nclass Vis(Base2):\n    pass\n"
_HIDDEN = "class B3:\n    pass\n\n\nclass Hid2(B3):\n    pass\n"

_VISIBLE_EDGE = ("proj.visible.Vis", "proj.visible.Base2")
_HIDDEN_EDGE = ("proj.ignored.hidden.Hid2", "proj.ignored.hidden.B3")


def _make_repo(root: Path, *, cgrignore: str | None) -> None:
    (root / "ignored").mkdir(parents=True)
    (root / "visible.py").write_text(_VISIBLE, encoding="utf-8")
    (root / "ignored" / "hidden.py").write_text(_HIDDEN, encoding="utf-8")
    if cgrignore is not None:
        (root / ".cgrignore").write_text(cgrignore, encoding="utf-8")


def test_the_oracle_without_cgrignore_sees_both(tmp_path: Path) -> None:
    """The control. Without it, exclusion and an empty fixture look alike."""
    src = tmp_path / "proj"
    _make_repo(src, cgrignore=None)

    inherits = oracle_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in inherits, inherits
    assert _HIDDEN_EDGE in inherits, inherits


def test_the_oracle_honours_cgrignore(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src, cgrignore="ignored/\n")

    inherits = oracle_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in inherits, "the fixture stopped producing any row"
    assert _HIDDEN_EDGE not in inherits, inherits


def test_cgr_without_cgrignore_sees_both(tmp_path: Path) -> None:
    """The control for the cgr side."""
    src = tmp_path / "proj"
    _make_repo(src, cgrignore=None)

    inherits = cgr_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in inherits, inherits
    assert _HIDDEN_EDGE in inherits, inherits


def test_cgr_honours_cgrignore(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _make_repo(src, cgrignore="ignored/\n")

    inherits = cgr_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in inherits, "the fixture stopped producing any row"
    assert _HIDDEN_EDGE not in inherits, inherits


def test_both_sides_agree_under_cgrignore(tmp_path: Path) -> None:
    """The halves must move together or grading regresses.

    Excluding only the cgr capture drops true positives while the oracle
    still emits the ignored row, which scores as a recall regression and
    reads like a grading bug rather than a scope fix. A test that changes
    one side would look like it caught something and have made it worse.
    """
    src = tmp_path / "proj"
    _make_repo(src, cgrignore="ignored/\n")

    oracle = oracle_inheritance(src, "proj").inherits
    cgr = cgr_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in oracle, "the oracle stopped producing any row"
    assert _VISIBLE_EDGE in cgr, "cgr stopped producing any row"
    assert _HIDDEN_EDGE not in oracle, oracle
    assert _HIDDEN_EDGE not in cgr, cgr
    assert oracle == cgr, oracle ^ cgr


_RESCUED = "class B4:\n    pass\n\n\nclass Res(B4):\n    pass\n"
_RESCUED_EDGE = ("proj.build.keep.Res", "proj.build.keep.B4")


def test_an_unignored_file_under_a_built_in_ignored_dir_reaches_both(
    tmp_path: Path,
) -> None:
    """The case a second filter cannot rescue.

    `build/` is a built-in ignore, and `should_skip_path` applies those only
    AFTER `unignore_paths` has had its say, so `!build/keep.py` reaches the
    graph. A pre-filter running before it drops the file from the oracle
    only, which is the oracle-versus-cgr divergence this issue closes,
    reintroduced one layer up (CodeRabbit, PR #1563).

    The visible edge is asserted alongside so a fixture that produced
    nothing cannot satisfy this by emptiness.
    """
    src = tmp_path / "proj"
    (src / "build").mkdir(parents=True)
    (src / "visible.py").write_text(_VISIBLE, encoding="utf-8")
    (src / "build" / "keep.py").write_text(_RESCUED, encoding="utf-8")
    (src / ".cgrignore").write_text("!build/keep.py\n", encoding="utf-8")

    oracle = oracle_inheritance(src, "proj").inherits
    cgr = cgr_inheritance(src, "proj").inherits

    assert _VISIBLE_EDGE in oracle, "the fixture stopped producing any row"
    assert _RESCUED_EDGE in oracle, f"the oracle dropped the rescued file: {oracle}"
    assert _RESCUED_EDGE in cgr, f"cgr dropped the rescued file: {cgr}"


# The located-supertype arms (Java, C#, C++, TypeScript) key a subclass by
# `file:line`, so one class per file at a fixed line gives a known key.
_JAVA_VISIBLE = "class Base2 {}\n\nclass Vis extends Base2 {}\n"
_JAVA_HIDDEN = "class B3 {}\n\nclass Hid extends B3 {}\n"
_CPP_VISIBLE = "class Base2 {};\n\nclass Vis : public Base2 {};\n"
_CPP_HIDDEN = "class B3 {};\n\nclass Hid : public B3 {};\n"


def _located_repo(root: Path, visible: str, hidden: str, suffix: str) -> None:
    (root / "ignored").mkdir(parents=True)
    (root / f"vis{suffix}").write_text(visible, encoding="utf-8")
    (root / "ignored" / f"hid{suffix}").write_text(hidden, encoding="utf-8")
    (root / ".cgrignore").write_text("ignored/\n", encoding="utf-8")


@pytest.mark.skipif(not java_available(), reason="java oracle needs a working JDK")
def test_the_java_oracle_honours_cgrignore(tmp_path: Path) -> None:
    """Only the TypeScript arm filtered its oracle; javac walked every file.

    So every edge in an excluded directory scored as a MISS: cgr never
    indexed the file, the oracle still emitted it.
    """
    src = tmp_path / "proj"
    _located_repo(src, _JAVA_VISIBLE, _JAVA_HIDDEN, ".java")

    oracle = java_oracle_inheritance(src)
    result = score_inheritance(java_cgr_inheritance(src, "proj"), oracle)

    assert ("vis.java:3", "Base2") in oracle.inherits, (
        "the fixture stopped producing any row"
    )
    assert ("ignored/hid.java:3", "B3") not in oracle.inherits, oracle.inherits
    assert all(not bucket["missing"] for bucket in result.diff.values()), result.diff


@pytest.mark.skipif(not cpp_available(), reason="libclang not available")
def test_the_cpp_oracle_honours_cgrignore(tmp_path: Path) -> None:
    src = tmp_path / "proj"
    _located_repo(src, _CPP_VISIBLE, _CPP_HIDDEN, ".cpp")
    # Absolute source paths: libclang parses from the process cwd, and a
    # relative one yields zero nodes silently.
    (src / "compile_commands.json").write_text(
        json.dumps(
            [
                {
                    "directory": str(src),
                    "command": f"clang++ -std=c++17 -c {source}",
                    "file": str(source),
                }
                for source in (src / "vis.cpp", src / "ignored" / "hid.cpp")
            ]
        ),
        encoding="utf-8",
    )

    oracle = cpp_oracle_inheritance(src)
    result = score_inheritance(cpp_cgr_inheritance(src, "proj"), oracle)

    assert ("vis.cpp:3", "Base2") in oracle.inherits, (
        "the fixture stopped producing any row"
    )
    assert ("ignored/hid.cpp:3", "B3") not in oracle.inherits, oracle.inherits
    assert all(not bucket["missing"] for bucket in result.diff.values()), result.diff
