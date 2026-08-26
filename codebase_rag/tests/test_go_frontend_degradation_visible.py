# A per-module tool failure must be VISIBLE, not merely survivable (#1462).
#
# `run_go_frontend` merges facts across every Go module anchor, and a failed
# anchor drops only its own module to the heuristics -- a deliberate choice
# (test_go_frontend.py::test_one_failing_module_degrades_only_itself), and the
# right one: a repo with ten modules and one wedged build should not lose the
# other nine.
#
# What was missing is the record. `_run_tool_once` returned `_empty_facts()` on
# failure, which is indistinguishable from "this module genuinely has no facts
# to report", so the resulting graph was compiler-accurate for some modules and
# heuristic for others with nothing saying which.
#
# That is the same distinction the protocol already draws correctly for call
# sites: `external_sites` exists so "resolved to nothing" and "not analysed"
# are different facts. This applies it at the module level.
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import codebase_rag.parsers.go_frontend.frontend as fe
from codebase_rag.parsers.go_frontend import run_go_frontend

_PAYLOAD = json.dumps(
    {
        "calls": [
            {
                "file": "s.go",
                "line": 3,
                "col": 1,
                "name": "Helper",
                "tfile": "s.go",
                "tline": 9,
                "tcol": 5,
            }
        ],
        "external": [],
        "implements": [],
    }
)


def _two_module_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/root\n\ngo 1.22\n")
    (repo / "sub" / "go.mod").write_text("module example.com/sub\n\ngo 1.22\n")
    return repo


def _patch_toolchain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fe.shutil, "which", lambda _name: "/usr/bin/go")
    monkeypatch.setattr(fe, "_build_tool", lambda _go: Path("/fake/gotypes"))


def test_a_failed_anchor_is_recorded_in_the_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The surviving module's facts must carry the fact that one module did not.

    Without this, a consumer cannot tell a repository where every module was
    analysed from one where half the tool runs timed out, and a query that
    returns fewer results looks like a correct answer about a smaller
    codebase.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    def _run(cmd, **_kwargs):
        if Path(cmd[1]) == repo:
            raise subprocess.TimeoutExpired(cmd, 1)
        return subprocess.CompletedProcess(cmd, 0, stdout=_PAYLOAD, stderr="")

    monkeypatch.setattr(fe.subprocess, "run", _run)

    facts = run_go_frontend(repo)

    assert facts.degraded_modules, "a failed anchor left no record"
    assert facts.call_sites, "the surviving module's facts were discarded"


def test_a_fully_successful_run_records_no_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paired control, and the one that gives the field meaning.

    A field that is always populated says nothing. Asserting only that it is
    non-empty on failure would pass against an implementation that marked
    every run degraded.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    monkeypatch.setattr(
        fe.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(
            cmd, 0, stdout=_PAYLOAD, stderr=""
        ),
    )

    facts = run_go_frontend(repo)

    assert not facts.degraded_modules, facts.degraded_modules


def test_a_module_with_no_facts_is_not_recorded_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty is not failure -- the distinction the bug erased.

    A module the tool analysed successfully and found nothing in must not be
    reported as degraded, or the field becomes noise and stops being read.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    empty = json.dumps({"calls": [], "external": [], "implements": []})
    monkeypatch.setattr(
        fe.subprocess,
        "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout=empty, stderr=""),
    )

    facts = run_go_frontend(repo)

    assert not facts.degraded_modules, facts.degraded_modules
