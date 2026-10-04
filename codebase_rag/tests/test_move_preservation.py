# move (issue #1534) review findings from PR #2907: each test is a move that
# used to commit while changing what the program does -- a name rebound, a
# constant lost, an optional import made required, an export dropped, or a
# concurrent edit overwritten.

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from codebase_rag.editing.move import MoveRefused
from codebase_rag.editing.transaction import load_history
from codebase_rag.tests.test_move_op import FIXTURE, _index, _materialise
from codebase_rag.tests.test_move_safety import _move

move_mod = importlib.import_module("codebase_rag.editing.move")


# --- the transaction's baseline is what the move was planned from ----------------


def _edit_after_planning(
    monkeypatch: pytest.MonkeyPatch, path: Path, text: str
) -> None:
    """Write `text` to `path` once `plan` has read the tree, before staging."""
    plan = move_mod.Mover.plan

    def plan_then_edit(self, *args, **kwargs):
        planned = plan(self, *args, **kwargs)
        path.write_text(text)
        return planned

    monkeypatch.setattr(move_mod.Mover, "plan", plan_then_edit)


def test_an_edit_made_after_planning_refuses_the_move(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stage` records the CURRENT bytes as the baseline, so a file edited
    between planning and staging was overwritten with content built from
    the bytes the plan read, and the edit was lost without a conflict."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    edited = FIXTURE["pkg/a.py"] + "\n\ndef later():\n    return 'kept'\n"
    _edit_after_planning(monkeypatch, root / "pkg/a.py", edited)
    with pytest.raises(MoveRefused, match="pkg/a.py changed on disk"):
        _move(root, store, updater)
    assert (root / "pkg/a.py").read_text() == edited
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert not (root / "pkg/core.py").exists()
    assert load_history(root) == []


def test_a_destination_created_after_planning_refuses_the_move(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan found no destination, so it wrote the whole file; one that
    appeared in the meantime was replaced by it."""
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    created = "VERSION = 2\n"
    _edit_after_planning(monkeypatch, root / "pkg/core.py", created)
    with pytest.raises(MoveRefused, match="pkg/core.py changed on disk"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == created
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert load_history(root) == []
