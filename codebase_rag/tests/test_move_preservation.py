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
from codebase_rag.tests.test_move_safety import OTHER, _move, _python

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


# --- imports stay in the scope they were written in -------------------------------


def test_an_import_inside_the_moved_function_is_not_copied_to_the_top(
    temp_repo: Path,
) -> None:
    """The import table holds function-local imports too, and every one
    whose name the moved text mentions was copied to the destination's top
    level: an import the function ran only when asked became one the
    module ran on load."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "def helper(enabled):\n"
        "    if enabled:\n"
        "        import optional_backend\n\n"
        "        return optional_backend.run()\n"
        "    return 'off'\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert report.copied_imports == ()
    core = (root / "pkg/core.py").read_text()
    assert core.startswith("def helper(enabled):\n")
    assert "        import optional_backend\n" in core
    probe = _python(root, "from pkg.core import helper; print(helper(False))")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "off"


# --- names bound under module-level control flow ----------------------------------


@pytest.mark.parametrize(
    ("prelude", "call", "expected"),
    [
        pytest.param(
            "import sys\n\nif sys.maxsize > 0:\n    SEP = '-'\nelse:\n    SEP = '+'\n",
            "helper(['a', 'b'])",
            "a-b",
            id="if-else",
        ),
        pytest.param(
            "try:\n    import optional_backend as SEP\n"
            "except ImportError:\n    SEP = '-'\n",
            "helper(['a', 'b'])",
            "a-b",
            id="try-except-import",
        ),
    ],
)
def test_a_name_bound_in_both_branches_travels_with_the_function(
    temp_repo: Path, prelude: str, call: str, expected: str
) -> None:
    """Only top-level simple statements counted as module bindings, so a
    constant set in both branches of an `if` (or by a `try` and its handler)
    was left behind and the moved helper died with a NameError. The `try`
    form also copied its guarded import unconditionally."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        prelude + "\n\ndef helper(a):\n    return SEP.join(a)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    assert report.copied_imports == ()
    assert "from pkg.util import SEP\n" in (root / "pkg/core.py").read_text()
    probe = _python(root, f"from pkg.core import helper; print({call})")
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == expected


@pytest.mark.parametrize(
    "prelude",
    [
        pytest.param(
            "import os\n\nif os.environ.get('UNSET'):\n    SEP = '-'\n", id="if"
        ),
        pytest.param("for SEP in ['-']:\n    pass\n", id="for"),
        pytest.param(
            "try:\n    import optional_backend as SEP\nexcept ImportError:\n    pass\n",
            id="try",
        ),
    ],
)
def test_a_name_that_may_be_unbound_refuses_the_move(
    temp_repo: Path, prelude: str
) -> None:
    """`from pkg.util import SEP` at the destination would fail on load
    wherever the old module left SEP unbound, where before only a call
    reaching it failed: the move cannot carry it, so it refuses."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        prelude + "\n\ndef helper(a):\n    return SEP.join(a)\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="SEP"):
        _move(root, store, updater)
    assert (root / "pkg/util.py").read_text() == fixture["pkg/util.py"]
    assert not (root / "pkg/core.py").exists()


def test_a_local_name_that_a_module_loop_also_binds_does_not_refuse(
    temp_repo: Path,
) -> None:
    """Only names the moved code reads from the module scope count: its own
    `i` is a local, whatever a top-level loop binds."""
    fixture = dict(FIXTURE)
    fixture["pkg/util.py"] = (
        "TOTAL = 0\nfor i in range(3):\n    TOTAL += i\n\n\n"
        "def helper(a):\n    return [i for i in a]\n" + OTHER
    )
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    probe = _python(root, "from pkg.core import helper; print(helper(['a']))")
    assert probe.returncode == 0, probe.stderr


# --- what the move adds must not rebind a destination name ------------------------

RATE_CORE = "RATE = 0.1\n\n\ndef rate():\n    return RATE\n"
RATE_HELPER = "\n\ndef helper(a):\n    return a * RATE\n" + OTHER


@pytest.mark.parametrize(
    ("util", "core", "extra"),
    [
        pytest.param(
            "RATE = 0.5\n" + RATE_HELPER, RATE_CORE, {}, id="old-module-constant"
        ),
        pytest.param(
            "from pkg.cfg import RATE\n" + RATE_HELPER,
            RATE_CORE,
            {"pkg/cfg.py": "RATE = 0.5\n"},
            id="copied-import",
        ),
        pytest.param(
            "RATE = 0.5\n" + RATE_HELPER,
            "from pkg.cfg import RATE\n\n\ndef rate():\n    return RATE\n",
            {"pkg/cfg.py": "RATE = 0.1\n"},
            id="destination-import",
        ),
    ],
)
def test_an_import_that_would_rebind_a_destination_name_refuses_the_move(
    temp_repo: Path, util: str, core: str, extra: dict[str, str]
) -> None:
    """Only the moved name was checked against the destination, so the
    `from pkg.util import RATE` pasted for the helper silently replaced the
    destination's own RATE, and its `rate()` started answering 0.5."""
    fixture = {**FIXTURE, "pkg/util.py": util, "pkg/core.py": core, **extra}
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    with pytest.raises(MoveRefused, match="already binds RATE"):
        _move(root, store, updater)
    assert (root / "pkg/core.py").read_text() == core
    assert (root / "pkg/util.py").read_text() == util
    probe = _python(root, "from pkg.core import rate; print(rate())")
    assert probe.stdout.strip() == "0.1", probe.stderr


def test_an_import_the_destination_already_has_is_not_a_collision(
    temp_repo: Path,
) -> None:
    """Binding a name to what it is already bound to replaces nothing."""
    core = "import os\n\n\ndef sep():\n    return os.sep\n"
    fixture = {**FIXTURE, "pkg/core.py": core}
    root = _materialise(temp_repo, fixture)
    store, updater = _index(root)
    report = _move(root, store, updater)
    assert report.applied, report.message
    probe = _python(
        root, "from pkg.core import helper, sep; print(helper(['a']), sep())"
    )
    assert probe.returncode == 0, probe.stderr
