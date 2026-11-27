# Issue #1227: the build lock shared by the Go, C# and Java frontends must
# survive a holder killed at any point. It is an OS-level file lock (flock /
# msvcrt.locking), so the kernel releases it on process death and no stale
# lock can exist; these tests prove mutual exclusion against a real second
# process and automatic release when that process is SIGKILLed. Issue #3794:
# a lock that cannot be created at all is told apart from one another process
# holds, and only the second is waited for.
import errno
import os
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.config import settings
from codebase_rag.parsers import build_lock
from codebase_rag.parsers.build_lock import acquire_build_lock, release_build_lock
from codebase_rag.parsers.csharp_frontend import frontend as csharp_fe
from codebase_rag.parsers.go_frontend import frontend as go_fe
from codebase_rag.parsers.java_frontend import frontend as java_fe
from codebase_rag.tests.conftest import needs_binding_permissions

_HOLDER_SCRIPT = textwrap.dedent(
    """
    import sys, time
    from pathlib import Path
    from codebase_rag.parsers.build_lock import acquire_build_lock

    handle = acquire_build_lock(Path(sys.argv[1]), lambda: False, 1, 0.0)
    print("locked" if handle else "busy", flush=True)
    if handle:
        time.sleep(600)
    """
)


def _spawn_holder(lock: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SCRIPT, str(lock)],
        stdout=subprocess.PIPE,
        text=True,
        encoding=cs.ENCODING_UTF8,
    )
    assert proc.stdout is not None
    reported = proc.stdout.readline().strip()
    # A holder that reports nothing died before taking the lock; its exit
    # status (negative: the signal that killed it) tells that apart from a
    # lock that was busy.
    assert reported == "locked", (reported, proc.wait(timeout=30))
    return proc


class _Polls:
    """Stands in for build_lock's `time`: each poll is recorded, not slept,
    so a test sees how long a wait would have been without taking it."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def time(self) -> float:
        return time.time()


@pytest.fixture
def polls(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    fake = _Polls()
    monkeypatch.setattr(build_lock, "time", fake)
    return fake.slept


def test_acquire_then_release_frees_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    release_build_lock(handle)
    again = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert again is not None
    release_build_lock(again)


def test_live_holder_excludes_other_processes(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    holder = _spawn_holder(lock)
    try:
        assert (
            acquire_build_lock(lock, lambda: False, tries=3, poll_seconds=0.05) is None
        )
    finally:
        holder.kill()
        holder.wait()


def test_killed_holder_releases_the_lock(tmp_path: Path) -> None:
    # The original #1227 failure mode: a holder SIGKILLed mid-build. The OS
    # releases the file lock with the process, so the next worker acquires
    # immediately instead of waiting out a retry budget forever.
    lock = tmp_path / ".build-lock"
    holder = _spawn_holder(lock)
    holder.kill()
    holder.wait()
    deadline = time.time() + 30
    handle = None
    while handle is None and time.time() < deadline:
        handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    release_build_lock(handle)


def test_waiter_yields_to_fresh_artifact(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    holder = _spawn_holder(lock)
    try:
        assert (
            acquire_build_lock(lock, lambda: True, tries=50, poll_seconds=0.01) is None
        )
    finally:
        holder.kill()
        holder.wait()


def test_dead_legacy_holder_directory_is_cleared(tmp_path: Path) -> None:
    # A crashed pre-#1227 holder left a mkdir-lock DIRECTORY at this path;
    # the file lock must displace it instead of failing to open forever.
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    if os.name == "posix":
        proc = subprocess.run(
            [sys.executable, "-c", "import os; print(os.getpid())"],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            check=True,
        )
        (lock / "pid").write_text(proc.stdout.strip())
    else:
        stale = time.time() - 3600
        os.utime(lock, (stale, stale))
    handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    assert lock.is_file()
    release_build_lock(handle)


def test_old_bare_legacy_directory_is_cleared(tmp_path: Path) -> None:
    # The released mkdir design wrote nothing inside the directory, so a
    # bare leftover is reclaimed once it has outlived any plausible build.
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    stale = time.time() - 3600
    os.utime(lock, (stale, stale))
    handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    assert lock.is_file()
    release_build_lock(handle)


def test_live_legacy_holder_keeps_the_path_busy(
    tmp_path: Path, polls: list[float]
) -> None:
    # A mixed-version upgrade window: an old-version builder still holds the
    # mkdir lock. Its directory must survive; acquisition just times out.
    # The directory refuses the open (EISDIR, or EACCES on Windows), which
    # must read as a held lock and be waited for, not as one that cannot be
    # created (#3794).
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    assert acquire_build_lock(lock, lambda: False, tries=3, poll_seconds=0.0) is None
    assert lock.is_dir()
    assert polls == [0.0, 0.0, 0.0]


@pytest.mark.parametrize("bad_pid", ["0", "-5", str(10**100), "not-a-pid"])
def test_unusable_legacy_pid_falls_back_to_age(tmp_path: Path, bad_pid: str) -> None:
    # pid 0 probes the caller's own process group (always alive), negatives
    # target groups, and an oversized int raises OverflowError from os.kill;
    # none identifies a holder, so only the age heuristic may reclaim.
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text(bad_pid)
    assert acquire_build_lock(lock, lambda: False, tries=2, poll_seconds=0.0) is None
    assert lock.is_dir()
    stale = time.time() - 3600
    os.utime(lock, (stale, stale))
    handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    assert lock.is_file()
    release_build_lock(handle)


def test_a_probe_emitting_non_utf8_reports_unavailable_rather_than_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A toolchain whose version banner is not UTF-8 must answer False.

    `toolchain_runs` decodes with strict UTF-8 (`encoding=ENCODING_UTF8`) and
    catches only `OSError` / `SubprocessError`. `UnicodeDecodeError` is
    neither -- it is a `ValueError` -- so a single non-UTF-8 byte from
    `java -version` propagates out of an availability CHECK.

    That is the wrong failure mode for this function specifically: every
    caller asks "can I use this toolchain?" and handles False by falling back
    to another frontend or skipping an oracle. An exception instead of False
    turns a graceful degradation into a crash, and the trigger is a locale or
    a vendor JDK printing a non-ASCII byte in its banner (CodeRabbit, #1480).

    Drives the real `toolchain_runs` against a real subprocess rather than
    mocking the decode, because the defect is in the decode configuration and
    a mock would encode my assumption about where it happens.
    """
    from codebase_rag.parsers.build_lock import toolchain_runs

    probe = tmp_path / ("probe.bat" if sys.platform == "win32" else "probe.py")
    if sys.platform == "win32":
        pytest.skip("shell quoting differs; the defect is platform-independent")
    probe.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        # 0x80 is a continuation byte with no lead byte: invalid UTF-8.
        "sys.stdout.buffer.write(b'openjdk version \\x80\\n')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)

    # `toolchain_runs` resolves via `shutil.which`, so the probe must be on PATH.
    # `monkeypatch` restores PATH even if the assertion below raises, which a
    # hand-rolled try/finally has to re-implement per test.
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    result = toolchain_runs(probe.name, timeout=10.0)

    assert result is True, (
        "the probe exits 0, so the toolchain IS available; a non-UTF-8 byte in "
        "its banner must not turn that into an exception or a False"
    )


def test_a_programming_error_in_the_probe_is_not_swallowed_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler must catch environment failures, not every failure.

    `toolchain_runs` answers "can I use this toolchain?", and its callers
    read False as "fall back to another frontend". That makes the exception
    list an UPPER bound as well as a lower one: `OSError` and
    `SubprocessError` are the environment saying no, and returning False for
    them is right. A `TypeError` from a mis-called API or an
    `AttributeError` after a refactor is this code being wrong, and
    reporting that as "toolchain unavailable" hides a bug behind a plausible
    answer -- callers would silently degrade forever with nothing to find.

    Guards the WIDENING rather than the fix. The preceding test pins that a
    non-UTF-8 banner is tolerated; without this one, broadening the handler
    to a bare `except Exception` passes the whole file. Measured: it does.
    A fix that loosens what is accepted needs a test for what must still be
    refused, or it has no upper bound (raised by peer review on #1485).
    """
    from codebase_rag.parsers import build_lock

    def _exploding_run(*args: object, **kwargs: object) -> object:
        raise TypeError("subprocess.run() got an unexpected keyword argument")

    monkeypatch.setattr(build_lock.shutil, "which", lambda _binary: "/usr/bin/true")
    monkeypatch.setattr(build_lock.subprocess, "run", _exploding_run)

    with pytest.raises(TypeError):
        build_lock.toolchain_runs("anything", timeout=1.0)


@dataclass(frozen=True)
class _Frontend:
    """A frontend that builds its tool under the shared lock: its module, the
    one file a repository needs for it to go as far as the build, and its
    entry point."""

    module: ModuleType
    repo_file: str
    repo_text: str
    entry: str

    def repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / self.repo_file).write_text(self.repo_text, encoding=cs.ENCODING_UTF8)
        return repo

    def cache(self) -> Path:
        return self.module._cache_dir()

    def lock(self) -> Path:
        return self.cache() / self.module._BUILD_LOCK

    def run(self, repo: Path) -> tuple[bool, list[str]]:
        """Whether a run on `repo` found any call site, and what it warned."""
        warnings: list[str] = []
        sink = logger.add(warnings.append, level="WARNING", format="{message}")
        try:
            facts = getattr(self.module, self.entry)(repo)
        finally:
            logger.remove(sink)
        return bool(facts.call_sites), [w.rstrip("\n") for w in warnings]


_FRONTENDS = [
    pytest.param(
        _Frontend(go_fe, "go.mod", "module example.com/demo\n", "run_go_frontend"),
        id="go",
    ),
    pytest.param(
        _Frontend(
            csharp_fe,
            "Demo.csproj",
            '<Project Sdk="Microsoft.NET.Sdk" />\n',
            "run_csharp_frontend",
        ),
        id="csharp",
    ),
    pytest.param(
        _Frontend(java_fe, "Demo.java", "class Demo {}\n", "run_java_frontend"),
        id="java",
    ),
]


class _FakeToolchain:
    """Stands in for go, dotnet and javac. Go's version probe gets the
    helper's own floor; every other command is a build, which fails, or
    times out."""

    def __init__(self) -> None:
        self.time_out = False
        self.builds: list[list[str]] = []

    def __call__(
        self, cmd: Sequence[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if list(cmd[1:]) == ["env", "GOVERSION"]:
            floor = go_fe._dotted(go_fe._required_go_version() or (1,))
            return subprocess.CompletedProcess(cmd, 0, stdout=f"go{floor}\n")
        self.builds.append(list(cmd))
        if self.time_out:
            raise subprocess.TimeoutExpired(cmd, timeout=1)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="build failed")


@pytest.fixture
def cgr_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "cgr_home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    return home


@pytest.fixture
def toolchain(monkeypatch: pytest.MonkeyPatch) -> _FakeToolchain:
    fake = _FakeToolchain()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def _refuse_lock(monkeypatch: pytest.MonkeyPatch, lock: Path, code: int) -> None:
    """Make the OS refuse to create `lock` with `code`, as it does for root
    too on a read-only file system."""
    real_open = os.open

    def refusing_open(path: str | Path, flags: int, *args: int, **kwargs: int) -> int:
        if Path(path) == lock:
            raise OSError(code, os.strerror(code), str(lock))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(build_lock.os, "open", refusing_open)


@needs_binding_permissions
@pytest.mark.parametrize("frontend", _FRONTENDS)
def test_a_read_only_cache_folder_falls_back_at_once(
    frontend: _Frontend,
    tmp_path: Path,
    cgr_home: Path,
    toolchain: _FakeToolchain,
    polls: list[float],
) -> None:
    # The lock file cannot be made in the folder, and no holder will ever
    # free it: the build waited out its five minutes of polls, then fell back
    # with nothing said (#3794).
    repo = frontend.repo(tmp_path)
    frontend.cache().mkdir(parents=True)
    frontend.cache().chmod(0o555)
    try:
        found, warnings = frontend.run(repo)
    finally:
        frontend.cache().chmod(0o755)

    error = OSError(errno.EACCES, os.strerror(errno.EACCES), str(frontend.lock()))
    assert polls == []
    assert warnings == [ls.FRONTEND_BUILD_LOCK_UNUSABLE.format(error=error)]
    assert not found
    assert toolchain.builds == []


@pytest.mark.parametrize("code", [errno.EACCES, errno.EROFS], ids=["eacces", "erofs"])
@pytest.mark.parametrize("frontend", _FRONTENDS)
def test_a_lock_the_os_refuses_falls_back_at_once_and_is_retried(
    frontend: _Frontend,
    code: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cgr_home: Path,
    toolchain: _FakeToolchain,
    polls: list[float],
) -> None:
    # The same refusal faked, so it runs as root, which writes a read-only
    # folder anyway. Nothing is remembered, as for a build that timed out:
    # the next run asks the OS again.
    repo = frontend.repo(tmp_path)
    _refuse_lock(monkeypatch, frontend.lock(), code)

    first, warnings = frontend.run(repo)
    second, later = frontend.run(repo)

    error = OSError(code, os.strerror(code), str(frontend.lock()))
    expected = ls.FRONTEND_BUILD_LOCK_UNUSABLE.format(error=error)
    assert polls == []
    assert warnings == [expected]
    assert later == [expected]
    assert not first
    assert not second
    assert toolchain.builds == []
    assert [path.name for path in cgr_home.rglob("*")] == [frontend.cache().name]


@pytest.mark.parametrize("frontend", _FRONTENDS)
def test_a_lock_whose_folder_is_a_file_falls_back_at_once(
    frontend: _Frontend,
    tmp_path: Path,
    cgr_home: Path,
    toolchain: _FakeToolchain,
    polls: list[float],
) -> None:
    repo = frontend.repo(tmp_path)
    frontend.cache().parent.mkdir(parents=True)
    frontend.cache().write_text("", encoding=cs.ENCODING_UTF8)

    found, warnings = frontend.run(repo)

    assert polls == []
    assert len(warnings) == 1
    assert warnings[0].startswith(ls.FRONTEND_BUILD_LOCK_UNUSABLE.partition("{")[0])
    # An OSError quotes its file as repr does, which doubles a Windows
    # path's backslashes; the error's own wording differs by platform.
    assert repr(str(frontend.cache())) in warnings[0]
    assert not found
    assert toolchain.builds == []


@pytest.mark.parametrize("frontend", _FRONTENDS)
def test_a_lock_another_process_holds_is_waited_for(
    frontend: _Frontend,
    tmp_path: Path,
    cgr_home: Path,
    toolchain: _FakeToolchain,
    polls: list[float],
) -> None:
    # Only a held lock is worth the wait: its holder frees it when its build
    # ends. A wait that runs out says so, once.
    repo = frontend.repo(tmp_path)
    frontend.cache().mkdir(parents=True)
    holder = _spawn_holder(frontend.lock())
    try:
        found, warnings = frontend.run(repo)
    finally:
        holder.kill()
        holder.wait()

    tries, poll = frontend.module._LOCK_TRIES, frontend.module._LOCK_POLL_SECONDS
    assert polls == [poll] * tries
    assert warnings == [
        ls.FRONTEND_BUILD_LOCK_TIMED_OUT.format(
            lock=frontend.lock(), seconds=tries * poll
        )
    ]
    assert not found
    assert toolchain.builds == []


@pytest.mark.parametrize("time_out", [False, True], ids=["fails", "times-out"])
@pytest.mark.parametrize("frontend", _FRONTENDS)
def test_a_failed_build_under_the_lock_warns_once(
    frontend: _Frontend,
    time_out: bool,
    tmp_path: Path,
    cgr_home: Path,
    toolchain: _FakeToolchain,
) -> None:
    # Each frontend names its own failed build, and nothing repeats it: the
    # C# frontend warned a second time whenever its tool was missing,
    # whatever the cause, so a refused lock and a timed-out build each gave
    # two warnings.
    repo = frontend.repo(tmp_path)
    toolchain.time_out = time_out

    found, warnings = frontend.run(repo)

    assert len(toolchain.builds) == 1
    assert len(warnings) == 1
    assert not found
