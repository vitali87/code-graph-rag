# A toolchain probe that timed out was recorded as `absent` (follow-up to
# #2634). The parser fingerprint asks each active toolchain for its version
# (`go version`, `javac -version`, `dotnet --version`) under a 10 s timeout,
# and a timeout answered the same as a missing binary. On a loaded machine one
# probe answers and the next does not, so two fingerprints computed back to
# back differed: the #2634 stability test flaked on Windows CI, and a user's
# sync after one slow probe (a cold JVM or dotnet start, an antivirus scan of
# go.exe) warned that the parser changed and re-parsed every file, with no
# toolchain change at all.
#
# A timeout means the tool could not be asked, not that it is missing. These
# tests pin what replaces the guess: an answer remembered for the same binary
# and pins, one longer retry, and failing both, a value that compares as
# unchanged. The negative half pins what must still change the fingerprint.
from __future__ import annotations

import errno
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag import parser_fingerprint as pf
from codebase_rag.capture import CaptureSelection, resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

GO_1_26 = "go version go1.26.6 linux/amd64"
GO_1_27 = "go version go1.27.0 linux/amd64"
# Outcomes a scripted probe can give besides a version banner.
TIMED_OUT = "<timed out>"
# The OS could not start the process for want of a resource, as
# CreateProcess does on a loaded Windows runner.
NOT_STARTED = "<not started>"
# The OS could not start the process and never will: a corrupt binary.
CANNOT_EXECUTE = "<cannot execute>"
EXIT_FAILURE = 1

_ONLY_GO_ACTIVE = {"GO_VERSION": True, "JAVAC_VERSION": False, "DOTNET_VERSION": False}


class ScriptedRun:
    """`subprocess.run` as the fingerprint's probes see it.

    Each call takes the next scripted outcome and the last one repeats, so a
    script of one outcome answers every call the same way. The timeout each
    call was given is recorded, which is how a test tells the first attempt
    from the retry, and an answer served from a cache from a probe that ran.
    """

    def __init__(self) -> None:
        self.outcomes: list[str | int] = [GO_1_26]
        self.timeouts: list[float] = []

    def script(self, *outcomes: str | int) -> None:
        self.outcomes = list(outcomes)
        self.timeouts = []

    def __call__(
        self, cmd: list[str], **kwargs: float | str | bool | None
    ) -> subprocess.CompletedProcess[str]:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, float)
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if outcome == TIMED_OUT:
            raise subprocess.TimeoutExpired(cmd, timeout)
        if outcome == NOT_STARTED:
            raise OSError(errno.ENOMEM, os.strerror(errno.ENOMEM))
        if outcome == CANNOT_EXECUTE:
            raise OSError(errno.ENOEXEC, os.strerror(errno.ENOEXEC))
        if isinstance(outcome, int):
            return subprocess.CompletedProcess(cmd, outcome, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{outcome}\n", stderr="")


@pytest.fixture
def go_binary(tmp_path: Path) -> Path:
    # A real file, so the binary has a stat identity a cache can key on.
    binary = tmp_path / "toolchain" / "go"
    binary.parent.mkdir()
    binary.write_bytes(b"go 1.26.6")
    return binary


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "outer" / "repo"
    root.mkdir(parents=True)
    (root / "module_a.py").write_text("def func_a():\n    pass\n", encoding="utf-8")
    return root


@pytest.fixture
def state(tmp_path: Path) -> Path:
    directory = tmp_path / "state"
    directory.mkdir()
    return directory


@pytest.fixture
def go_probe(monkeypatch: pytest.MonkeyPatch, go_binary: Path) -> ScriptedRun:
    """Only Go is active, and only the fingerprint's own probes are scripted.

    The fingerprint module's `shutil` and `subprocess` names are swapped for
    stand-ins rather than patching the modules themselves, so a sync running
    under this fixture still runs every other subprocess and lookup for real.
    `which` finds the binary only while the file exists, as on a real PATH.
    """
    scripted = ScriptedRun()

    def which(name: str) -> str | None:
        return str(go_binary) if name == "go" and go_binary.is_file() else None

    monkeypatch.setattr(pf, "shutil", SimpleNamespace(which=which))
    monkeypatch.setattr(
        pf,
        "subprocess",
        SimpleNamespace(
            run=scripted,
            TimeoutExpired=subprocess.TimeoutExpired,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    monkeypatch.setattr(pf, "_active_tools", lambda repo_path=None: _ONLY_GO_ACTIVE)
    return scripted


@pytest.fixture
def warnings_sink() -> Iterator[list[str]]:
    messages: list[str] = []
    handler_id = logger.add(
        lambda m: messages.append(str(m)), level="WARNING", format="{message}"
    )
    yield messages
    logger.remove(handler_id)


def _as_a_new_process(monkeypatch: pytest.MonkeyPatch) -> None:
    # What a new process remembers of the probes an earlier one ran: nothing.
    monkeypatch.setattr(pf, "_PROBE_ANSWERS", {}, raising=False)


def _sync(
    repo: Path, ingestor: MagicMock, capture: CaptureSelection | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=capture,
    )
    updater.run()
    return updater


def _mismatch_warnings(messages: list[str]) -> list[str]:
    return [m for m in messages if ls.PARSER_FINGERPRINT_MISMATCH in m]


def _replace_binary(binary: Path, content: bytes) -> None:
    binary.write_bytes(content)
    # Some filesystems keep a coarse mtime; a later one makes the change
    # visible to the stat identity whatever the clock's resolution.
    later = binary.stat().st_mtime_ns + 5_000_000_000
    os.utime(binary, ns=(later, later))


class TestATimedOutProbeIsNotAMissingTool:
    def test_a_probe_that_times_out_after_answering_keeps_the_fingerprint(
        self, go_probe: ScriptedRun
    ) -> None:
        """The CI flake itself: the same process, the same toolchain, twice.

        `test_fingerprint_is_stable_while_ast_grep_availability_is` computes
        the fingerprint twice in a row; on a loaded Windows runner the second
        `go version` missed its timeout and the two digests differed.
        """
        go_probe.script(GO_1_26, TIMED_OUT)

        first = pf.compute_parser_fingerprint()
        second = pf.compute_parser_fingerprint()

        assert second == first

    def test_a_later_sync_whose_probe_times_out_reports_no_parser_change(
        self,
        go_probe: ScriptedRun,
        repo: Path,
        mock_ingestor: MagicMock,
        warnings_sink: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The product bug: one slow probe in a later process re-parsed it all.

        The first sync stamps a fingerprint with Go's answer. The next sync
        runs in a fresh process whose probe times out; it must reuse the
        answer the earlier process got for the same binary, not record Go as
        missing and warn that the parser changed.
        """
        _sync(repo, mock_ingestor)
        _as_a_new_process(monkeypatch)
        go_probe.script(TIMED_OUT)

        later = _sync(repo, mock_ingestor)

        assert _mismatch_warnings(warnings_sink) == []
        assert later._parser_changed is False
        # Answered from the earlier process's answer: one bounded attempt and
        # no retry.
        assert go_probe.timeouts == [cs.TOOL_VERSION_PROBE_TIMEOUT]

    def test_a_timed_out_probe_with_nothing_cached_is_asked_again_for_longer(
        self, go_probe: ScriptedRun, repo: Path
    ) -> None:
        go_probe.script(TIMED_OUT, GO_1_26)

        answer = pf._probe_version("go", "version", repo)

        assert answer == GO_1_26
        assert go_probe.timeouts == [
            cs.TOOL_VERSION_PROBE_TIMEOUT,
            cs.TOOL_VERSION_PROBE_RETRY_TIMEOUT,
        ]

    def test_a_probe_that_never_answers_is_unknown_not_absent(
        self, go_probe: ScriptedRun, repo: Path
    ) -> None:
        go_probe.script(TIMED_OUT)

        answer = pf._probe_version("go", "version", repo)

        assert answer == cs.TOOL_VERSION_UNKNOWN
        assert len(go_probe.timeouts) == 2

    def test_a_probe_that_did_not_start_before_answering_keeps_the_fingerprint(
        self, go_probe: ScriptedRun
    ) -> None:
        """The shape both #2634 CI failures had: `absent`, then the version.

        Their digests reproduce with `GO_VERSION=absent` in the first
        computation and the real `go version` in the second, and the old code
        gave `absent` for a process that could not be started as well as for a
        timeout. A first attempt the OS refused is asked again.
        """
        go_probe.script(NOT_STARTED, GO_1_26)

        first = pf.compute_parser_fingerprint()
        second = pf.compute_parser_fingerprint()

        assert second == first
        assert f"GO_VERSION={GO_1_26}" in pf._tool_versions()

    def test_a_probe_that_did_not_start_after_answering_keeps_the_fingerprint(
        self, go_probe: ScriptedRun
    ) -> None:
        go_probe.script(GO_1_26, NOT_STARTED)

        first = pf.compute_parser_fingerprint()
        second = pf.compute_parser_fingerprint()

        assert second == first
        # The refused attempt was answered from this process's memo, with no
        # retry.
        assert go_probe.timeouts == [
            cs.TOOL_VERSION_PROBE_TIMEOUT,
            cs.TOOL_VERSION_PROBE_TIMEOUT,
        ]

    def test_a_probe_that_never_starts_is_unknown_not_absent(
        self, go_probe: ScriptedRun, repo: Path
    ) -> None:
        go_probe.script(NOT_STARTED)

        answer = pf._probe_version("go", "version", repo)

        assert answer == cs.TOOL_VERSION_UNKNOWN
        assert len(go_probe.timeouts) == 2

    def test_an_unanswered_probe_compares_as_unchanged(
        self,
        go_probe: ScriptedRun,
        repo: Path,
        mock_ingestor: MagicMock,
        warnings_sink: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing cached and both attempts time out: no change, no warning.

        The stamp is a digest, so `unknown` cannot be skipped inside it; the
        fingerprint hashes the value this checkout last hashed for the tool
        instead, which is what "unchanged" means. A real change is not lost:
        the stamp is left as it was, so the next sync that can ask compares
        the real answer against it.
        """
        first = _sync(repo, mock_ingestor)
        (first.state_dir / cs.TOOL_VERSION_PROBE_CACHE_FILENAME).unlink()
        _as_a_new_process(monkeypatch)
        go_probe.script(TIMED_OUT)

        later = _sync(repo, mock_ingestor)

        assert _mismatch_warnings(warnings_sink) == []
        assert later._parser_changed is False


class TestRealChangesStillChangeTheFingerprint:
    def test_a_removed_binary_is_absent_and_changes_the_fingerprint(
        self, go_probe: ScriptedRun, go_binary: Path, repo: Path, state: Path
    ) -> None:
        with_go = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)
        go_binary.unlink()

        without_go = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)

        assert without_go != with_go
        assert "GO_VERSION=absent" in pf._tool_versions(repo, state)

    @pytest.mark.parametrize(
        "content",
        [b"go 1.27.0 with a longer banner", b"go 1.26.6"],
        ids=["new-size", "same-size-new-mtime"],
    )
    def test_a_changed_binary_is_probed_again_and_changes_the_fingerprint(
        self,
        go_probe: ScriptedRun,
        go_binary: Path,
        repo: Path,
        state: Path,
        content: bytes,
    ) -> None:
        before = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)
        _replace_binary(go_binary, content)
        go_probe.script(GO_1_27)

        after = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)

        assert after != before
        assert go_probe.timeouts == [cs.TOOL_VERSION_PROBE_TIMEOUT]

    def test_a_new_answer_from_an_unchanged_binary_changes_the_fingerprint(
        self, go_probe: ScriptedRun, repo: Path, state: Path
    ) -> None:
        # Codex review of PR 3368: a toolchain can change behind an
        # unchanged host binary and pins (an SDK installed beside `dotnet`),
        # so a long-lived process must ask again rather than serve the
        # answer it remembered.
        before = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)
        go_probe.script(GO_1_27)

        after = pf.compute_parser_fingerprint(repo_path=repo, state_dir=state)

        assert after != before
        assert f"GO_VERSION={GO_1_27}" in pf._tool_versions(repo, state)

    def test_a_changed_binary_is_never_answered_from_the_cache(
        self,
        go_probe: ScriptedRun,
        go_binary: Path,
        repo: Path,
        state: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The old binary's answer is on disk; the new binary times out once.
        assert pf._probe_version("go", "version", repo, state) == GO_1_26
        _replace_binary(go_binary, b"go 1.27.0 with a longer banner")
        _as_a_new_process(monkeypatch)
        go_probe.script(TIMED_OUT, GO_1_27)

        answer = pf._probe_version("go", "version", repo, state)

        assert answer == GO_1_27

    @pytest.mark.parametrize(
        "pin",
        ["go.work", "go.mod", "global.json", "../go.work"],
        ids=["go.work", "go.mod", "global.json", "parent-go.work"],
    )
    def test_a_changed_pin_is_not_hidden_by_the_cache(
        self, go_probe: ScriptedRun, repo: Path, state: Path, pin: str
    ) -> None:
        """Go and .NET pick a toolchain from files around the working dir.

        A `go.work` or `go.mod` line, or a `global.json`, in the repository
        or above it, can select a different toolchain for the same binary.
        Neither the in-process memo nor the on-disk answer may stand in for a
        probe once one of them changed.
        """
        pin_file = repo / pin
        pin_file.write_text("go 1.26\n", encoding="utf-8")
        assert pf._probe_version("go", "version", repo, state) == GO_1_26
        pin_file.write_text("go 1.27\ntoolchain go1.27.0\n", encoding="utf-8")
        go_probe.script(TIMED_OUT, GO_1_27)

        answer = pf._probe_version("go", "version", repo, state)

        assert answer == GO_1_27

    def test_a_non_zero_exit_is_still_absent_even_with_an_answer_cached(
        self,
        go_probe: ScriptedRun,
        repo: Path,
        state: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assert pf._probe_version("go", "version", repo, state) == GO_1_26
        _as_a_new_process(monkeypatch)
        go_probe.script(EXIT_FAILURE)

        answer = pf._probe_version("go", "version", repo, state)

        assert answer == "absent"

    def test_a_non_zero_exit_is_not_remembered(
        self, go_probe: ScriptedRun, repo: Path
    ) -> None:
        # A binary that failed once is asked again next time, not held absent.
        go_probe.script(EXIT_FAILURE, GO_1_26)
        assert pf._probe_version("go", "version", repo) == "absent"

        answer = pf._probe_version("go", "version", repo)

        assert answer == GO_1_26

    def test_a_binary_that_cannot_execute_is_absent_not_remembered(
        self, go_probe: ScriptedRun
    ) -> None:
        """A permanent start failure is a real change, not a starved probe.

        A corrupt replacement leaves a binary `which` still finds, but neither
        the probe nor the frontend can run it, so the remembered version must
        not stand in for it (Codex review of PR 3371).
        """
        go_probe.script(GO_1_26, CANNOT_EXECUTE)

        first = pf.compute_parser_fingerprint()
        second = pf.compute_parser_fingerprint()

        assert second != first
        assert "GO_VERSION=absent" in pf._tool_versions()
        # Answered absent at once: no retry for a failure that cannot pass.
        assert go_probe.timeouts.count(cs.TOOL_VERSION_PROBE_RETRY_TIMEOUT) == 0

    def test_inactive_tools_are_not_probed_and_stay_inactive(
        self,
        go_probe: ScriptedRun,
        repo: Path,
        state: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Go's answer is cached and recorded as hashed; switching every tool
        # off must still read `inactive`, not a remembered version.
        pf._tool_versions(repo, state)
        none_active = dict.fromkeys(_ONLY_GO_ACTIVE, False)
        monkeypatch.setattr(pf, "_active_tools", lambda repo_path=None: none_active)
        go_probe.script(TIMED_OUT)

        versions = pf._tool_versions(repo, state)

        assert versions == [f"{key}=inactive" for key in _ONLY_GO_ACTIVE]
        assert go_probe.timeouts == []

    def test_an_unanswered_probe_does_not_hide_another_change(
        self,
        go_probe: ScriptedRun,
        repo: Path,
        mock_ingestor: MagicMock,
        warnings_sink: list[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`unknown` stands in for Go alone; every other input still counts.

        Skipping the comparison whenever a probe failed would be simpler, but
        a toolchain that never answers would then switch off the staleness
        check for parser code, grammars and capture selection for good.
        """
        first = _sync(repo, mock_ingestor)
        (first.state_dir / cs.TOOL_VERSION_PROBE_CACHE_FILENAME).unlink()
        _as_a_new_process(monkeypatch)
        go_probe.script(TIMED_OUT)

        later = _sync(repo, mock_ingestor, resolve_capture(["io"]))

        assert later._parser_changed is True
        assert len(_mismatch_warnings(warnings_sink)) == 1


class _WindowsStartError(OSError):
    """An OSError carrying a Windows code, on any platform the suite runs on."""

    winerror = 0


def _windows_start_error(code: int) -> OSError:
    exc = _WindowsStartError()
    exc.winerror = code
    return exc


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (OSError(errno.ENOMEM, "out of memory"), True),
        (OSError(errno.EAGAIN, "try again"), True),
        (OSError(errno.EMFILE, "too many open files"), True),
        (_windows_start_error(1455), True),
        (_windows_start_error(8), True),
        (OSError(errno.ENOEXEC, "exec format error"), False),
        (OSError(errno.ENOENT, "interpreter missing"), False),
        (OSError(errno.EACCES, "permission denied"), False),
        (_windows_start_error(193), False),
        (OSError("no errno at all"), False),
    ],
)
def test_only_resource_exhaustion_counts_as_a_probe_never_asked(
    exc: OSError, transient: bool
) -> None:
    assert pf._is_transient_start_failure(exc) is transient
