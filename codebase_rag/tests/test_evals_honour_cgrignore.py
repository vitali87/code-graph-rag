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

from pathlib import Path

from evals.inheritance import cgr_inheritance, oracle_inheritance

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

    assert _VISIBLE_EDGE in oracle and _VISIBLE_EDGE in cgr
    assert _HIDDEN_EDGE not in oracle
    assert _HIDDEN_EDGE not in cgr
