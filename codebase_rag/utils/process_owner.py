"""Whether the process that wrote a durable record may still be running.

The CLI sync's incomplete-run marker names the process that wrote it, so
`cgr delete-project` can tell a sync that stopped from one still writing the
project (PR #2532 review). Every answer errs towards "may be running": a
marker wrongly kept only lists the project as interrupted, while one wrongly
cleared leaves a partial graph that looks whole.

The sync lock file names its holder too, so a sync refused right after its
holder was killed can tell a lock the OS has yet to release from one a
running sync holds (follow-up to issue #2441).
"""

import os
import socket
import sys
from functools import cache
from pathlib import Path

from .. import constants as cs

if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
    import ctypes
    from ctypes import wintypes

    @cache
    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL(cs.WINDOWS_KERNEL32, use_last_error=True)
        # Declared rather than left to ctypes, which would pass and return
        # each HANDLE as a C int and cut a 64-bit handle short.
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        return kernel32

    def _windows_pid_has_exited(pid: int) -> bool:
        if not 0 < pid <= cs.WINDOWS_MAX_PID:
            return False
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(
            cs.WINDOWS_PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            # Only "no such pid" says the process is gone; access denied is
            # a running process of another user, or a protected one.
            return ctypes.get_last_error() == cs.WINDOWS_ERROR_INVALID_PARAMETER
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            # An exited process stays in the table, with its exit code set,
            # while any handle to it is open (its parent's, until reaped).
            return code.value != cs.WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding=cs.ENCODING_UTF8).strip()
    except OSError:
        return ""


def _read_link(path: str) -> str:
    try:
        return os.readlink(path)
    except OSError:
        return ""


@cache
def process_host() -> str:
    """The process table a recorded pid belongs to: machine and pid namespace.

    A hostname alone does not name one. Two machines can share a name, and
    containers with host networking share the host's while each keeps its own
    pid namespace, where the same pid is a different process.
    """
    return cs.PROCESS_HOST_SEPARATOR.join(
        (
            socket.gethostname(),
            _read_text(cs.PROCESS_BOOT_ID_PATH),
            _read_link(cs.PROCESS_PID_NAMESPACE_PATH),
        )
    )


@cache
def process_table_identified() -> bool:
    """Whether `process_host` names this process table and no other.

    Only where both the boot id and the pid namespace can be read, as on
    Linux. Elsewhere, or in a container that hides them, the name is the
    hostname alone, which another machine can carry too, so a pid recorded
    under it may be a process this table has never had (review of PR #3496).
    """
    return bool(_read_text(cs.PROCESS_BOOT_ID_PATH)) and bool(
        _read_link(cs.PROCESS_PID_NAMESPACE_PATH)
    )


def pid_may_be_running(pid: int) -> bool:
    """False only when no process with this pid exists in this process table.

    Probed on POSIX alone, as `parsers.build_lock` does: Windows' `os.kill`
    terminates the process rather than probing it, so there the answer stays
    "may be". A pid of zero or below names a process group, not a process.
    """
    if pid <= 0 or os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):
        # Alive under another user (EPERM), or not a pid the kernel takes.
        return True
    return True


def pid_has_exited(pid: int) -> bool:
    """True only when the process with this pid has provably exited.

    Unlike `pid_may_be_running`, Windows is probed too, and without
    signalling: a query handle from OpenProcess and its GetExitCodeProcess,
    since `os.kill` there terminates the process it names. Every doubt (a
    pid that names no single process, one this user may not open, a probe
    that fails) answers False.
    """
    if sys.platform == cs.PLATFORM_WINDOWS:  # pragma: no cover - platform
        return _windows_pid_has_exited(pid)
    return not pid_may_be_running(pid)
