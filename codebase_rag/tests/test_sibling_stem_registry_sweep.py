"""Deleting `a.py` must not sweep `a/b.py`'s registry entries (issue #1773).

A directory can share a sibling FILE's stem, and then the module qns nest:
`proj/a.py` records `proj.a` while `proj/a/b.py` records `proj.a.b`.
`remove_file_from_state` sweeps `function_registry` by module-qn prefix, so
deleting `a.py` matched `proj.a.` and took `b.py`'s definitions with it --
though `b.py` still exists and the deletion event does not re-parse it, so
nothing restores them. Calls into those definitions then stop resolving, and
a later re-parse can land a `@line` DUP_QN variant beside the row that was
never removed (the #1719 shape).

`foreign_qns` does not save them. It is built from function SPAN records, so
it protects `proj.a.b.Nested.deep` while leaving the CLASS `proj.a.b.Nested`,
which records no span. The issue measured that asymmetry on `A.cs` beside
`A/B.cs`; it reproduces identically here on `a.py` beside `a/b.py`, because
the sweep is language-agnostic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_A_PY = "def top():\n    return 1\n"
_B_PY = (
    "class Nested:\n"
    "    def deep(self):\n"
    "        return 2\n"
    "\n\n"
    "def sibling():\n"
    "    return 3\n"
)


def _build(tmp_path: Path, files: dict[str, str]) -> GraphUpdater:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(), repo_path=tmp_path, parsers=parsers, queries=queries
    )
    updater.run()
    return updater


def _qns(updater: GraphUpdater) -> set[str]:
    return set(updater.function_registry.keys())


def test_deleting_a_file_keeps_its_stem_sharing_directorys_definitions(
    tmp_path: Path,
) -> None:
    updater = _build(
        tmp_path,
        {"a.py": _A_PY, "a/__init__.py": "", "a/b.py": _B_PY},
    )
    project = tmp_path.name
    before = _qns(updater)
    survivors = {qn for qn in before if f"{project}.a.b" in qn}
    assert survivors, "fixture registered nothing for a/b.py"

    (tmp_path / "a.py").unlink()
    updater.remove_file_from_state(tmp_path / "a.py")

    after = _qns(updater)
    lost = sorted(survivors - after)
    assert not lost, f"a/b.py still exists but lost registry entries: {lost}"

    # The CLASS is the one the old sweep dropped: `foreign_qns` is built from
    # function span records, so it protected the method and not the class.
    # Naming it explicitly means a fix that only rescues methods fails here.
    assert f"{project}.a.b.Nested" in after
    assert f"{project}.a.b.Nested.deep" in after


def test_the_deleted_files_own_definitions_are_still_removed(tmp_path: Path) -> None:
    """The control, and the one that matters most.

    Keeping the surviving file's entries must not be achieved by weakening
    the sweep: the deleted file's own rows still have to go, or a stale
    definition steers resolution at something that no longer exists. A test
    asserting only the survivors would pass with the sweep deleted entirely.
    """
    updater = _build(
        tmp_path,
        {"a.py": _A_PY, "a/__init__.py": "", "a/b.py": _B_PY},
    )
    project = tmp_path.name
    assert f"{project}.a.top" in _qns(updater)

    (tmp_path / "a.py").unlink()
    updater.remove_file_from_state(tmp_path / "a.py")

    assert f"{project}.a.top" not in _qns(updater)


def test_a_plain_deletion_with_no_stem_collision_is_unaffected(tmp_path: Path) -> None:
    """The ordinary case must keep behaving ordinarily: with no directory
    sharing the stem, everything under the deleted module still goes."""
    updater = _build(
        tmp_path,
        {"solo.py": "class Gone:\n    def m(self):\n        return 1\n"},
    )
    project = tmp_path.name
    assert f"{project}.solo.Gone" in _qns(updater)

    (tmp_path / "solo.py").unlink()
    updater.remove_file_from_state(tmp_path / "solo.py")

    remaining = {qn for qn in _qns(updater) if qn.startswith(f"{project}.solo")}
    assert not remaining, f"deleted module left entries behind: {sorted(remaining)}"


def test_deleting_the_nested_file_does_not_disturb_the_shorter_module(
    tmp_path: Path,
) -> None:
    """The other direction: deleting `a/b.py` must leave `a.py` alone.

    `proj.a` is a PREFIX of `proj.a.b`, not the reverse, so this direction
    was never broken -- which is exactly why it belongs here. If a fix
    rescued survivors by keeping anything a longer module also matches, this
    would start failing.
    """
    updater = _build(
        tmp_path,
        {"a.py": _A_PY, "a/__init__.py": "", "a/b.py": _B_PY},
    )
    project = tmp_path.name

    (tmp_path / "a" / "b.py").unlink()
    updater.remove_file_from_state(tmp_path / "a" / "b.py")

    after = _qns(updater)
    assert f"{project}.a.top" in after, "deleting a/b.py wrongly removed a.py's entry"
    assert f"{project}.a.b.Nested" not in after
    assert f"{project}.a.b.sibling" not in after


@pytest.mark.parametrize("depth", [2, 3])
def test_deeper_stem_collisions_are_kept_too(tmp_path: Path, depth: int) -> None:
    """`a/b.py` beside `a/b/c.py` is the same shape one level down."""
    files = {"a/__init__.py": "", "a/b.py": _A_PY}
    nested = "a/" + "/".join(["b"] * (depth - 1)) + "/c.py"
    files["a/b/__init__.py"] = ""
    files[nested] = _B_PY
    updater = _build(tmp_path, files)
    project = tmp_path.name

    target = tmp_path / "a" / "b.py"
    # Anchor on the nested MODULE qn, not on a substring: the temp directory
    # name can itself contain "c", which made an earlier version of this
    # filter pick up `a.b.top` -- a definition from the file being DELETED --
    # and report it as a lost survivor.
    nested_module = f"{project}." + nested[: -len(".py")].replace("/", ".")
    survivors = {
        qn
        for qn in _qns(updater)
        if qn == nested_module or qn.startswith(f"{nested_module}.")
    }
    if not survivors:
        pytest.skip("fixture produced no nested entries at this depth")

    target.unlink()
    updater.remove_file_from_state(target)

    lost = sorted(survivors - _qns(updater))
    assert not lost, f"nested surviving file lost entries: {lost}"
