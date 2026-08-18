# Issue #1227: the mkdir build lock shared by the Go and C# frontends must
# survive a holder killed between mkdir and the releasing rmdir. A waiter
# reclaims the lock when the recorded holder pid is dead (POSIX) or, without
# a usable pid, when the lock has outlived any plausible build.
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codebase_rag.parsers import build_lock
from codebase_rag.parsers.build_lock import acquire_build_lock, release_build_lock


def _dead_pid() -> int:
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def test_acquire_records_holder_and_release_removes_lock(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    assert acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert lock.is_dir()
    assert int((lock / "pid").read_text()) == os.getpid()
    release_build_lock(lock)
    assert not lock.exists()


@pytest.mark.skipif(os.name != "posix", reason="pid liveness probe is POSIX-only")
def test_dead_holder_lock_is_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text(str(_dead_pid()))
    assert acquire_build_lock(lock, lambda: False, tries=2, poll_seconds=0.0)
    assert int((lock / "pid").read_text()) == os.getpid()
    release_build_lock(lock)


@pytest.mark.skipif(os.name != "posix", reason="pid liveness probe is POSIX-only")
def test_live_holder_lock_is_respected(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    assert not acquire_build_lock(lock, lambda: False, tries=2, poll_seconds=0.0)
    assert lock.is_dir()


def test_pidless_lock_falls_back_to_age(tmp_path: Path, monkeypatch) -> None:
    # A holder killed between mkdir and the pid write (or a non-POSIX host)
    # leaves no usable pid; only an over-age lock may be reclaimed.
    monkeypatch.setattr(build_lock, "_holder_pid", lambda _lock: None)
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    assert not acquire_build_lock(lock, lambda: False, tries=2, poll_seconds=0.0)
    stale = time.time() - build_lock._LOCK_STALE_SECONDS - 60
    os.utime(lock, (stale, stale))
    assert acquire_build_lock(lock, lambda: False, tries=2, poll_seconds=0.0)
    release_build_lock(lock)


def test_waiter_yields_to_fresh_artifact(tmp_path: Path) -> None:
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    assert not acquire_build_lock(lock, lambda: True, tries=5, poll_seconds=0.0)
