"""Whether the process that wrote a durable record may still be running.

The CLI sync's incomplete-run marker names the process that wrote it, so
`cgr delete-project` can tell a sync that stopped from one still writing the
project (PR #2532 review). Every answer errs towards "may be running": a
marker wrongly kept only lists the project as interrupted, while one wrongly
cleared leaves a partial graph that looks whole.
"""

import os
import socket
from functools import cache
from pathlib import Path

from .. import constants as cs


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
