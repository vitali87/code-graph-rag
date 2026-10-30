"""Whether a recorded pid's process has provably exited (follow-up to #2441).

A sync refused right after its holder was killed retries the lock while the
holder the lock file names has exited, so the probe must tell an exited
process from a running one on every platform, and must never signal the
process it asks about: on Windows `os.kill` terminates it.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.utils.process_owner import pid_has_exited, process_table_identified


def _gone_pid() -> int:
    """The pid of a process that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait(timeout=60)
    return child.pid


@pytest.fixture
def running_pid() -> Iterator[int]:
    """The pid of a child process that runs until the test is done."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
    )
    assert child.stdin is not None
    try:
        yield child.pid
    finally:
        child.stdin.close()
        child.wait(timeout=60)


def test_a_reaped_child_has_exited() -> None:
    assert pid_has_exited(_gone_pid())


def test_this_process_has_not_exited() -> None:
    # Negative.
    assert not pid_has_exited(os.getpid())


def test_a_running_child_has_not_exited(running_pid: int) -> None:
    # Negative.
    assert not pid_has_exited(running_pid)


@pytest.mark.parametrize(
    "pid",
    [0, -1, 2**40],
    ids=["zero", "negative", "past-the-pid-range"],
)
def test_a_pid_naming_no_single_process_has_not_exited(pid: int) -> None:
    # Negative: 0 and below name process groups on POSIX (and 0 Windows'
    # idle process), and a pid past the platform's range cannot be probed,
    # so none of them proves anything gone.
    assert not pid_has_exited(pid)


@pytest.mark.skipif(
    sys.platform != cs.PLATFORM_WINDOWS,
    reason="only Windows' os.kill terminates the process instead of probing it",
)
def test_windows_never_signals_the_process_it_asks_about(running_pid: int) -> None:
    with patch(
        "codebase_rag.utils.process_owner.os.kill",
        side_effect=AssertionError("os.kill terminates the process on Windows"),
    ):
        assert not pid_has_exited(running_pid)
        assert not pid_has_exited(os.getpid())
        assert pid_has_exited(_gone_pid())


# Review of PR #3496: a full sync retires another run's marker once that
# run's pid has exited here, which says something only when the host's name
# cannot also be another machine's or another pid namespace's.


@pytest.fixture
def fresh_identity() -> Iterator[None]:
    process_table_identified.cache_clear()
    yield
    process_table_identified.cache_clear()


@pytest.mark.usefixtures("fresh_identity")
@pytest.mark.parametrize("missing", ["boot-id", "pid-namespace"])
def test_a_host_name_without_both_parts_identifies_no_process_table(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Negative: the hostname alone, or a container hiding its pid namespace.
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("6b1c2f0e\n", encoding="utf-8")
    absent = str(tmp_path / "absent")
    monkeypatch.setattr(
        cs, "PROCESS_BOOT_ID_PATH", absent if missing == "boot-id" else str(boot_id)
    )
    if missing == "pid-namespace":
        monkeypatch.setattr(cs, "PROCESS_PID_NAMESPACE_PATH", absent)

    assert not process_table_identified()


@pytest.mark.usefixtures("fresh_identity")
@pytest.mark.skipif(
    sys.platform == cs.PLATFORM_WINDOWS,
    reason="creating a symbolic link takes a privilege on Windows",
)
def test_a_boot_id_and_a_pid_namespace_identify_the_process_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Controlled paths, as Linux lays them out: a container that hides the
    # real ones is not identified, and must not fail this test.
    boot_id = tmp_path / "boot_id"
    boot_id.write_text("6b1c2f0e\n", encoding="utf-8")
    namespace = tmp_path / "pid"
    namespace.symlink_to("pid:[4026531836]")
    monkeypatch.setattr(cs, "PROCESS_BOOT_ID_PATH", str(boot_id))
    monkeypatch.setattr(cs, "PROCESS_PID_NAMESPACE_PATH", str(namespace))

    assert process_table_identified()
