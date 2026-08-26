# Both C++ pending-call lists must reset on every run (#1178 review).
#
# `_run_cpp_frontend` cleared `_pending_cpp_macro_calls` at the top but not
# `_pending_cpp_expansion_calls`. On any early return -- the mode is off,
# libclang is unavailable, no compile_commands.json -- a previous run's
# expansion calls survived into the next one, and the Pass-3 consumer at
# graph_updater.py:801 emitted CALLS edges for expansions that no longer
# apply.
#
# The failure is silent: the edges are plausible, they are simply about a
# state of the repository that no longer exists.
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.config import settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


def _updater(tmp_path: Path) -> GraphUpdater:
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=MagicMock(), repo_path=tmp_path, parsers=parsers, queries=queries
    )


def test_expansion_calls_reset_when_the_frontend_does_not_run(
    tmp_path: Path, monkeypatch
) -> None:
    """Stale expansion calls must not survive a run that never produced any.

    Asserting on the macro list alone would pass today: it is already reset.
    The expansion list is the one that leaked, and both are asserted so a
    future reset of one without the other is caught.
    """
    updater = _updater(tmp_path)
    updater._pending_cpp_macro_calls = ["stale-macro"]  # type: ignore[list-item]
    updater._pending_cpp_expansion_calls = ["stale-expansion"]  # type: ignore[list-item]

    # TREESITTER takes the earliest return, before any libclang probing.
    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.TREESITTER)
    updater._run_cpp_frontend()

    assert updater._pending_cpp_macro_calls == []
    assert updater._pending_cpp_expansion_calls == []


def test_covered_files_also_reset(tmp_path: Path, monkeypatch) -> None:
    """The control: the reset block clears every piece of per-run state.

    Without this, a change that reset the two lists but dropped the covered
    set would pass the test above while leaking a different piece of the same
    state.
    """
    updater = _updater(tmp_path)
    updater._cpp_frontend_covered = frozenset({"stale.cpp"})

    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.TREESITTER)
    updater._run_cpp_frontend()

    assert updater._cpp_frontend_covered == frozenset()
