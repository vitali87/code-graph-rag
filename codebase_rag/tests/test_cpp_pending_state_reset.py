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


def test_covered_set_resets_before_pass_two_on_a_hybrid_rerun(
    tmp_path: Path, monkeypatch
) -> None:
    """A reused updater must not carry a LIBCLANG covered-set into HYBRID.

    In HYBRID mode `_run_cpp_frontend` runs AFTER Pass 2, so its reset is too
    late: `_process_files` consumes the covered set to skip files, and a stale
    entry makes it skip a file whose Module subtree was just deleted. The file
    then gets only a generic `File` node, and the later hybrid pass cannot
    restore the tree-sitter definitions it never produced.

    Asserting on the covered set after `run()` rather than on the emitted
    nodes keeps this about the state, which is what the ordering bug is.
    """
    (tmp_path / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=MagicMock(), repo_path=tmp_path, parsers=parsers, queries=queries
    )
    # Simulate a previous LIBCLANG run on the same instance.
    updater._cpp_frontend_covered = frozenset({"stale.cpp"})

    monkeypatch.setattr(settings, "CPP_FRONTEND", cs.CppFrontend.HYBRID)

    # The value AT PASS 2 is what matters: _run_cpp_frontend runs after it in
    # HYBRID and resets the set on the way out, so asserting after run()
    # returns would pass against the bug.
    seen: list[frozenset[str]] = []
    original = updater._process_files

    def _record(*args, **kwargs):
        seen.append(updater._cpp_frontend_covered)
        return original(*args, **kwargs)

    monkeypatch.setattr(updater, "_process_files", _record)
    updater.run()

    assert seen, "_process_files was never called"
    assert seen[0] == frozenset(), seen[0]
