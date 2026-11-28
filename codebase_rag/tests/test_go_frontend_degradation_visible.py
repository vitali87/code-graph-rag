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
import sys
from pathlib import Path

import pytest
from loguru import logger

import codebase_rag.parsers.go_frontend.frontend as fe
from codebase_rag import logs as ls
from codebase_rag.parsers.go_frontend import run_go_frontend

# Kept before a test replaces `subprocess.run`, so a fake helper can still
# start a real process and have `subprocess` decode its output exactly as the
# frontend asks it to, on every platform.
_REAL_RUN = subprocess.run
# Deeper than the limit any interpreter puts on decoding nested JSON.
_DEPTH = 100_000

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


def _call_payload(line: str) -> str:
    """A one-call payload whose call is on `line`, spelled as JSON text."""
    return (
        '{"calls": [{"file": "s.go", "line": ' + line + ', "col": 1, '
        '"name": "Helper", "tfile": "s.go", "tline": 9, "tcol": 5}]}'
    )


def _printing(stdout: bytes, stderr: bytes) -> list[str]:
    """A command that prints these bytes as they are."""
    script = (
        "import sys; "
        f"sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r})"
    )
    return [sys.executable, "-c", script]


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


def test_a_crashed_tool_is_recorded_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit is a failure, not an empty result.

    The tool's own exit code was never checked, so a crashed run whose stdout
    happened to parse -- or was simply empty -- looked exactly like a module
    with nothing to report.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    def _run(cmd, **_kwargs):
        if Path(cmd[1]) == repo:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout=_PAYLOAD, stderr="")

    monkeypatch.setattr(fe.subprocess, "run", _run)

    facts = run_go_frontend(repo)

    assert facts.degraded_modules, "a crashed tool left no record"
    assert facts.call_sites, "the surviving module's facts were discarded"


@pytest.mark.parametrize(
    ("stdout", "why"),
    [
        ("", "no output at all"),
        ("not json at all", "unparseable JSON"),
        ("[1, 2, 3]", "well-formed JSON of the wrong shape"),
        ('{"calls": [{"file": "s.go"}]}', "a malformed fact entry"),
        # Each of the next three is JSON `json.loads` reads or refuses with
        # an error other than JSONDecodeError, which ended the index (#3792).
        pytest.param(
            _call_payload("Infinity"), "a position of Infinity", id="infinity"
        ),
        pytest.param(
            _call_payload("9" * (sys.get_int_max_str_digits() + 1)),
            "an integer past the int-string limit",
            id="huge-integer",
        ),
        pytest.param(
            '{"calls": ' + "[" * _DEPTH + "]" * _DEPTH + "}",
            "nesting past the recursion limit",
            id="deep-nesting",
        ),
    ],
)
def test_every_parse_failure_is_recorded_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, why: str
) -> None:
    """Each parse-failure path must degrade visibly, not silently.

    All four returned empty facts, which is the answer for a module that was
    analysed and had nothing -- so a tool contract violation was indingishable
    from a quiet success. Parametrised because they are separate returns and
    fixing one would otherwise leave the rest silent.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    def _run(cmd, **_kwargs):
        if Path(cmd[1]) == repo:
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=_PAYLOAD, stderr="")

    monkeypatch.setattr(fe.subprocess, "run", _run)

    facts = run_go_frontend(repo)

    assert facts.degraded_modules, f"{why} left no record"


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    [
        pytest.param(_call_payload("3").encode() + b"\n", b"caf\xe9\n", id="stderr"),
        pytest.param(
            _call_payload("3").replace("s.go", "caf\xe9.go").encode("latin-1"),
            b"",
            id="stdout",
        ),
    ],
)
def test_output_that_is_not_utf8_is_a_failed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes, stderr: bytes
) -> None:
    """A helper whose output is not UTF-8 failed, like one that crashed.

    The strict decode raised out of `subprocess.run` and ended the index
    (#3792). Read leniently instead, the byte in a payload's file name would
    have read as U+FFFD and joined nothing, so the module's facts must go,
    with one warning, and the other module's stay.
    """
    repo = _two_module_repo(tmp_path)
    _patch_toolchain(monkeypatch)

    def _run(cmd, **kwargs):
        if Path(cmd[1]) == repo:
            return _REAL_RUN(_printing(stdout, stderr), **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=_PAYLOAD, stderr="")

    monkeypatch.setattr(fe.subprocess, "run", _run)
    warnings: list[str] = []
    sink = logger.add(warnings.append, level="WARNING", format="{message}")
    try:
        facts = run_go_frontend(repo)
    finally:
        logger.remove(sink)

    assert facts.degraded_modules == (".",)
    assert facts.analysed_modules == ("sub",)
    assert set(facts.call_sites) == {("sub/s.go", 3, 1, "Helper")}
    assert [w.rstrip("\n") for w in warnings] == [
        ls.GO_FRONTEND_RUN_NOT_UTF8,
        ls.GO_FRONTEND_ANCHOR_DEGRADED.format(anchor=repo),
    ]
