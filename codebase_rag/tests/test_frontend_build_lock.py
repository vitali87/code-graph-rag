# Issue #1227: the build lock shared by the Go and C# frontends must survive
# a holder killed at any point. It is an OS-level file lock (flock /
# msvcrt.locking), so the kernel releases it on process death and no stale
# lock can exist; these tests prove mutual exclusion against a real second
# process and automatic release when that process is SIGKILLed.
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from codebase_rag.parsers.build_lock import acquire_build_lock, release_build_lock

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
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "locked"
    return proc


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


def test_legacy_mkdir_lock_directory_is_cleared(tmp_path: Path) -> None:
    # A crashed pre-#1227 holder left a mkdir-lock DIRECTORY at this path;
    # the file lock must displace it instead of failing to open forever.
    lock = tmp_path / ".build-lock"
    lock.mkdir()
    (lock / "pid").write_text("12345")
    handle = acquire_build_lock(lock, lambda: False, tries=1, poll_seconds=0.0)
    assert handle is not None
    assert lock.is_file()
    release_build_lock(handle)
