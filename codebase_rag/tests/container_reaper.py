"""Reap integration-suite Memgraph containers a killed run left behind (#1628).

`memgraph_container` stops its container after `yield`, which is correct for
every run that ends normally. A run killed by an OOM kill, a CI timeout or a
`kill -9` never reaches that line, and nothing in-process can: eight such
containers were found running on one box, one per killed run, the oldest three
weeks old and each a permanent charge against the memory the next run needs.

The remedy that survives the process dying is external to it: label our
containers with a marker of our own AND the session that owns them (host and
pid), and at the START of a session remove every labelled container whose
owner is gone. Ownership is what makes this safe when two sessions share one
Docker daemon: a container whose owning pid is still alive on this host is a
running suite's database, not a corpse, and one owned by another host cannot
be judged from here at all, so both are left alone (#1751 review).
"""

from __future__ import annotations

import os
import socket
import subprocess
from collections.abc import Callable
from typing import Protocol

from loguru import logger

from codebase_rag import constants as cs

CGR_CONTAINER_LABEL = "cgr.integration"
CGR_CONTAINER_LABEL_VALUE = "memgraph"
CGR_OWNER_HOST_LABEL = "cgr.integration.host"
CGR_OWNER_PID_LABEL = "cgr.integration.pid"
CGR_OWNER_STARTED_LABEL = "cgr.integration.started"

REAPER_REMOVED = "Removed orphaned integration container(s): {names}"
REAPER_REMOVE_FAILED = "Could not remove orphaned integration container {name}: {error}"
REAPER_KEPT_LIVE = (
    "Integration container {name} belongs to live pid {pid} on this host; kept"
)


class _Removable(Protocol):
    name: str
    labels: dict[str, str]

    def remove(self, force: bool = ...) -> None: ...


class _Containers(Protocol):
    def list(self, all: bool = ..., filters: dict[str, str] | None = ...) -> list: ...


class ContainerClient(Protocol):
    containers: _Containers


def cgr_container_labels(
    *,
    host: str | None = None,
    pid: int | None = None,
    started: str | None = None,
) -> dict[str, str]:
    """The labels a session puts on its own container: marker plus owner.

    The owner is host, pid AND the process start time: a killed suite's pid
    can be handed to an unrelated process later, and a pid alone would then
    read the corpse as owned by a live session forever (#1751 review).
    """
    owner_pid = pid if pid is not None else os.getpid()
    return {
        CGR_CONTAINER_LABEL: CGR_CONTAINER_LABEL_VALUE,
        CGR_OWNER_HOST_LABEL: host if host is not None else socket.gethostname(),
        CGR_OWNER_PID_LABEL: str(owner_pid),
        CGR_OWNER_STARTED_LABEL: (
            started if started is not None else process_start_time(owner_pid) or ""
        ),
    }


def process_start_time(pid: int) -> str | None:
    """`ps`'s start time for `pid`, or None where it cannot be read.

    `lstart` is the full timestamp on both BSD and GNU ps, so it changes
    when a pid is reused. Windows has no `ps`; None there, and the reaper
    then has nothing to compare, which is the same conservative answer as
    an unprobeable pid.
    """
    if os.name == "nt":
        return None
    try:
        completed = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            encoding=cs.ENCODING_UTF8,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    started = completed.stdout.strip()
    return started or None


def pid_is_alive(pid: int) -> bool:
    """Whether `pid` is a running process on THIS host.

    Signal 0 probes without delivering anything; a pid we may not signal is
    still alive. A pid the probe cannot even express (an `OverflowError` on
    a value past the platform's pid range, or any other `OSError`) is
    reported alive too, so a garbled label keeps its container rather than
    aborting the fixture before `start()` (CodeRabbit, #1751). On Windows
    `os.kill` cannot probe (any signal other than the two console events
    terminates the target), so a pid there is reported alive and its
    container is never reaped: leaving a corpse is the cheap error, killing
    a live suite's database the expensive one.
    """
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError, ValueError):
        return True
    return True


def reap_orphaned_containers(
    client: ContainerClient,
    *,
    host: str | None = None,
    pid_alive: Callable[[int], bool] = pid_is_alive,
    start_time: Callable[[int], str | None] = process_start_time,
) -> list[str]:
    """Remove every labelled container whose owning session is gone.

    `all=True` so a stopped-but-not-removed corpse goes too. A container is
    removed only when its owner labels name THIS host and a process that is
    no longer running here: the pid is gone, or the pid is held by a process
    whose start time differs from the one recorded at labelling, which is a
    reused pid and not the owner (#1751 review). Another host's container,
    one without owner labels, or one whose owner is alive is kept: the first
    two cannot be judged from here, and the third is a running suite. Where
    a start time cannot be read on either side the pid check alone decides,
    in the keeping direction. A container that refuses to go (already gone,
    or an engine error) is logged and skipped rather than failing the
    session: the reaper is housekeeping, not a precondition.
    """
    this_host = host if host is not None else socket.gethostname()
    removed: list[str] = []
    label = f"{CGR_CONTAINER_LABEL}={CGR_CONTAINER_LABEL_VALUE}"
    for container in client.containers.list(all=True, filters={"label": label}):
        name = str(getattr(container, "name", ""))
        labels = getattr(container, "labels", None) or {}
        if labels.get(CGR_OWNER_HOST_LABEL) != this_host:
            continue
        try:
            owner_pid = int(labels.get(CGR_OWNER_PID_LABEL, ""))
        except ValueError:
            continue
        if pid_alive(owner_pid) and not _pid_was_reused(
            owner_pid, labels.get(CGR_OWNER_STARTED_LABEL), start_time
        ):
            logger.info(REAPER_KEPT_LIVE.format(name=name, pid=owner_pid))
            continue
        try:
            container.remove(force=True)
        except Exception as e:  # noqa: BLE001 -- housekeeping must not fail the run
            logger.warning(REAPER_REMOVE_FAILED.format(name=name, error=e))
            continue
        removed.append(name)
    if removed:
        logger.info(REAPER_REMOVED.format(names=", ".join(removed)))
    return removed


def _pid_was_reused(
    pid: int, recorded_start: str | None, start_time: Callable[[int], str | None]
) -> bool:
    if not recorded_start:
        return False
    current = start_time(pid)
    return current is not None and current != recorded_start
