from __future__ import annotations

import errno
import itertools
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import IO
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from loguru import logger

from codebase_rag import constants as root_cs
from codebase_rag.stack import cli as stack_cli
from codebase_rag.stack import constants as cs
from codebase_rag.stack import health
from codebase_rag.stack.manager import StackError, StackManager

# What mgclient's C transport prints for each failed read or write (issue
# #2356).
MGCLIENT_NOISE = "mg_raw_transport"
STDERR_FD = 2
NEVER_READY_TIMEOUT_S = 0.2

# Docker's port proxy accepts a connection on the published port while the
# Memgraph behind it is still starting, then drops it once the proxy cannot
# reach the container: with a reset or a plain close, both covered here. It
# runs in its own process because mgclient holds the GIL while it waits on the
# socket, so a server thread in the test process would never get to answer.
_STARTING_MEMGRAPH = """
import socket, struct, sys
listener = socket.create_server(("127.0.0.1", 0))
print(listener.getsockname()[1], flush=True)
while True:
    conn, _ = listener.accept()
    conn.recv(20)
    if sys.argv[1] == "reset":
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    conn.close()
"""


@pytest.fixture(params=["reset", "close"])
def starting_memgraph(request: pytest.FixtureRequest) -> Iterator[int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _STARTING_MEMGRAPH, request.param],
        stdout=subprocess.PIPE,
        text=True,
        encoding=root_cs.ENCODING_UTF8,
    )
    try:
        assert proc.stdout is not None
        yield int(proc.stdout.readline())
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def debug_records() -> Iterator[list[tuple[str, str]]]:
    records: list[tuple[str, str]] = []
    sink_id = logger.add(
        lambda message: records.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    try:
        yield records
    finally:
        logger.remove(sink_id)


@pytest.fixture
def stack_home(tmp_path: Path) -> Path:
    home = tmp_path / "cgr-home"
    home.mkdir()
    return home


def _manager(home: Path, port: int) -> StackManager:
    mgr = StackManager(
        home=home,
        package_compose=Path("/dev/null"),
        memgraph_host=cs.LOOPBACK_HOST,
        memgraph_port=port,
    )
    mgr.memgraph_credentials = None
    return mgr


@pytest.fixture
def quick_memgraph_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real wait, polling every 10ms instead of every second.
    monkeypatch.setattr(
        "codebase_rag.stack.manager.wait_for_memgraph",
        partial(health.wait_for_memgraph, interval=0.01),
    )


def _closed_port() -> int:
    with socket.create_server((cs.LOOPBACK_HOST, 0)) as listener:
        return listener.getsockname()[1]


def _noisy_connect(error: Exception) -> Callable[..., MagicMock]:
    """A connect that writes to fd 2 the way the C library does, then fails."""

    def connect(**_: object) -> MagicMock:
        os.write(STDERR_FD, f"{MGCLIENT_NOISE}: Connection reset by peer\n".encode())
        raise error

    return connect


# --- The issue: failed probes while Memgraph starts print to the terminal ---


def test_waiting_for_a_starting_memgraph_prints_nothing(
    starting_memgraph: int, capfd: pytest.CaptureFixture[str]
) -> None:
    assert not health.wait_for_memgraph(
        cs.LOOPBACK_HOST, starting_memgraph, timeout=0.2, interval=0.01
    )

    assert capfd.readouterr().err == ""


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        pytest.param(
            lambda port: health._bolt_reachable(
                cs.LOOPBACK_HOST, port, ("cgr", "s3cret")
            ),
            False,
            id="login-probe",
        ),
        pytest.param(
            lambda port: health.memgraph_anonymous_access(cs.LOOPBACK_HOST, port),
            cs.AnonymousAccess.NO_ANSWER,
            id="anonymous-access",
        ),
        pytest.param(
            lambda port: health.memgraph_rejects_credentials(
                cs.LOOPBACK_HOST, port, ("cgr", "s3cret")
            ),
            False,
            id="rejects-credentials",
        ),
    ],
)
def test_every_memgraph_probe_is_silent_while_memgraph_starts(
    probe: Callable[[int], object],
    expected: object,
    starting_memgraph: int,
    capfd: pytest.CaptureFixture[str],
) -> None:
    assert probe(starting_memgraph) == expected

    assert capfd.readouterr().err == ""


@pytest.mark.usefixtures("quick_memgraph_waits")
def test_daemon_up_waits_for_a_starting_memgraph_without_noise(
    starting_memgraph: int, stack_home: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # Nothing listens before `up -d`; after it the port proxy answers and
    # drops a few probes while Memgraph starts, and then Memgraph is ready.
    mgr = _manager(stack_home, starting_memgraph)
    real_connect = health.mgclient.connect
    closed_port = _closed_port()
    probes_after_start = itertools.count()
    started = threading.Event()

    def memgraph(**kwargs: object) -> MagicMock:
        if not started.is_set():
            return real_connect(host=cs.LOOPBACK_HOST, port=closed_port)
        if next(probes_after_start) < 3:
            return real_connect(**kwargs)
        return MagicMock()

    with (
        patch.object(stack_cli, "StackManager", return_value=mgr),
        patch.object(mgr, "locate_published_qdrant"),
        patch.object(mgr, "up", side_effect=started.set),
        patch.object(mgr, "_services_accepting_anonymous", return_value=[]),
        patch.object(mgr, "_raise_if_qdrant_rejects_key"),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        patch.object(health.mgclient, "connect", side_effect=memgraph),
    ):
        result = CliRunner().invoke(stack_cli.cli, ["up"])

    assert result.exit_code == 0, result.output
    assert f"state:    {cs.StackState.RUNNING.value}" in result.output
    assert next(probes_after_start) > 3
    assert capfd.readouterr().err.count(MGCLIENT_NOISE) == 0


# --- What must not change ---


@pytest.mark.usefixtures("quick_memgraph_waits")
def test_daemon_up_still_reports_a_memgraph_that_never_starts(
    starting_memgraph: int, stack_home: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    mgr = _manager(stack_home, starting_memgraph)

    with (
        patch.object(stack_cli, "StackManager", return_value=mgr),
        patch.object(mgr, "locate_published_qdrant"),
        patch.object(mgr, "up"),
        patch.object(
            mgr,
            "wait_healthy",
            side_effect=lambda: StackManager.wait_healthy(
                mgr, timeout=NEVER_READY_TIMEOUT_S
            ),
        ),
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
    ):
        result = CliRunner().invoke(stack_cli.cli, ["up"])

    assert result.exit_code == 1
    assert (
        cs.ERR_STACK_NOT_HEALTHY.format(
            service=cs.SERVICE_MEMGRAPH, timeout=NEVER_READY_TIMEOUT_S
        )
        in result.output
    )
    assert capfd.readouterr().err.count(MGCLIENT_NOISE) == 0


@pytest.mark.usefixtures("quick_memgraph_waits")
def test_waiting_for_a_memgraph_that_never_starts_still_raises(
    starting_memgraph: int, stack_home: Path
) -> None:
    mgr = _manager(stack_home, starting_memgraph)

    with (
        patch("codebase_rag.stack.manager.wait_for_qdrant", return_value=True),
        pytest.raises(StackError, match="did not become healthy"),
    ):
        mgr.wait_healthy(timeout=NEVER_READY_TIMEOUT_S)


# On Windows mgclient prints through a C runtime of its own and does not
# always emit its line, for a close as for a reset: a missing line there says
# nothing about where the message would have gone (seen on PR #3100's CI).
_MGCLIENT_STDERR_NOT_GUARANTEED = pytest.mark.skipif(
    sys.platform == "win32",
    reason="mgclient does not always emit stderr for a dropped connection on Windows",
)


@pytest.mark.parametrize(
    "starting_memgraph",
    [
        pytest.param("reset", marks=_MGCLIENT_STDERR_NOT_GUARANTEED),
        pytest.param("close", marks=_MGCLIENT_STDERR_NOT_GUARANTEED),
    ],
    indirect=True,
)
def test_the_c_library_message_is_kept_at_debug_level(
    starting_memgraph: int, debug_records: list[tuple[str, str]]
) -> None:
    # Where it runs, this sees the real message's level directly rather than
    # through capfd: on Windows mgclient prints through a C runtime of its
    # own, whose fd 2 capfd does not watch (and there the cases are skipped,
    # see _MGCLIENT_STDERR_NOT_GUARANTEED).
    health.memgraph_anonymous_access(cs.LOOPBACK_HOST, starting_memgraph)

    assert [level for level, text in debug_records if MGCLIENT_NOISE in text] == [
        "DEBUG"
    ]


def test_stderr_outside_a_probe_still_reaches_the_terminal(
    starting_memgraph: int, capfd: pytest.CaptureFixture[str]
) -> None:
    os.write(STDERR_FD, b"before\n")
    health._bolt_reachable(cs.LOOPBACK_HOST, starting_memgraph)
    os.write(STDERR_FD, b"after\n")

    assert capfd.readouterr().err == "before\nafter\n"


def test_an_unexpected_probe_error_propagates_and_restores_stderr(
    capfd: pytest.CaptureFixture[str],
) -> None:
    with (
        patch.object(
            health.mgclient, "connect", side_effect=_noisy_connect(RuntimeError("bug"))
        ),
        pytest.raises(RuntimeError, match="bug"),
    ):
        health._bolt_reachable(cs.LOOPBACK_HOST, 7687)
    os.write(STDERR_FD, b"after\n")

    assert capfd.readouterr().err == "after\n"


def test_concurrent_probes_leave_stderr_pointing_at_the_terminal(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # fd 2 is one per process: two probes swapping it at once without taking
    # turns would each restore the other's capture file as stderr.
    def slow_noisy_connect(**_: object) -> MagicMock:
        os.write(STDERR_FD, f"{MGCLIENT_NOISE}: connection closed\n".encode())
        time.sleep(0.01)
        raise health.mgclient.OperationalError("failed to receive handshake")

    with patch.object(health.mgclient, "connect", side_effect=slow_noisy_connect):
        threads = [
            threading.Thread(
                target=health._bolt_reachable, args=(cs.LOOPBACK_HOST, 7687)
            )
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    os.write(STDERR_FD, b"after\n")

    assert capfd.readouterr().err == "after\n"


def test_a_process_without_stderr_still_probes(
    debug_records: list[tuple[str, str]],
) -> None:
    # A service started with fd 2 closed: the C library's message still goes
    # to the debug log, and fd 2 is left closed.
    runtime = _SeparateCRuntime(stderr=None)

    def connect(**_: object) -> MagicMock:
        runtime.perror(f"{MGCLIENT_NOISE}_recv: connection closed by server")
        raise health.mgclient.OperationalError("failed to receive handshake")

    with (
        patch.object(health, "_PYTHON_C_RUNTIME", runtime),
        patch.object(health, "_mgclient_own_c_runtime", return_value=None),
        patch.object(health.mgclient, "connect", side_effect=connect),
    ):
        assert not health._bolt_reachable(cs.LOOPBACK_HOST, 7687)

    assert runtime.fds == {}
    assert [level for level, text in debug_records if MGCLIENT_NOISE in text] == [
        "DEBUG"
    ]


@pytest.mark.parametrize(
    ("error", "access", "rejected"),
    [
        ("Authentication failure", cs.AnonymousAccess.REFUSED, True),
        (
            "couldn't connect to host: Connection refused",
            cs.AnonymousAccess.NO_ANSWER,
            False,
        ),
    ],
)
def test_a_refused_login_is_still_told_apart_from_no_answer(
    error: str,
    access: cs.AnonymousAccess,
    rejected: bool,
    capfd: pytest.CaptureFixture[str],
) -> None:
    failure = health.mgclient.OperationalError(error)
    with patch.object(health.mgclient, "connect", side_effect=_noisy_connect(failure)):
        assert health.memgraph_anonymous_access(cs.LOOPBACK_HOST, 7687) is access
        assert (
            health.memgraph_rejects_credentials(
                cs.LOOPBACK_HOST, 7687, ("cgr", "s3cret")
            )
            is rejected
        )

    assert capfd.readouterr().err == ""


def test_a_ready_memgraph_is_still_reachable(
    capfd: pytest.CaptureFixture[str],
) -> None:
    with patch.object(health.mgclient, "connect") as connect:
        assert health.wait_for_memgraph(cs.LOOPBACK_HOST, 7687, timeout=1.0)

    connect.return_value.close.assert_called_once()
    assert capfd.readouterr().err == ""


# --- Windows: mgclient prints through a C runtime of its own ---


class _SeparateCRuntime:
    """A C runtime with descriptors of its own, as msvcrt.dll has on Windows.

    pymgclient's Windows wheels print through msvcrt.dll, while Python's os
    module works on the UCRT's descriptors, so os.dup2 onto fd 2 leaves the
    fd 2 that mgclient writes to where it was. Each descriptor here is backed
    by a real one but numbered apart from them.
    """

    def __init__(self, stderr: int | None) -> None:
        self.fds: dict[int, int] = {}
        if stderr is not None:
            self.fds[STDERR_FD] = os.dup(stderr)

    def _add(self, real_fd: int) -> int:
        # The lowest free number, so with fd 2 closed the next one is fd 2.
        fd = next(n for n in itertools.count(STDERR_FD) if n not in self.fds)
        self.fds[fd] = real_fd
        return fd

    def dup(self, fd: int) -> int:
        if fd not in self.fds:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        return self._add(os.dup(self.fds[fd]))

    def dup2(self, fd: int, fd2: int) -> None:
        real_fd = os.dup(self.fds[fd])
        if fd2 in self.fds:
            os.close(self.fds[fd2])
        self.fds[fd2] = real_fd

    def close(self, fd: int) -> None:
        os.close(self.fds.pop(fd))

    def open_file(self, file: IO[bytes]) -> int:
        return self._add(os.dup(file.fileno()))

    def perror(self, text: str) -> None:
        os.write(self.fds[STDERR_FD], f"{text}\n".encode())


@pytest.fixture
def mgclient_c_runtime(tmp_path: Path) -> Iterator[tuple[_SeparateCRuntime, Path]]:
    # As on Windows: the fd 2 mgclient prints to is a terminal of its own.
    terminal = tmp_path / "terminal"
    with terminal.open("wb") as file:
        runtime = _SeparateCRuntime(file.fileno())
    try:
        with patch.object(health, "_mgclient_own_c_runtime", return_value=runtime):
            yield runtime, terminal
    finally:
        for real_fd in runtime.fds.values():
            os.close(real_fd)


def test_mgclient_printing_through_its_own_c_runtime_stays_off_the_terminal(
    mgclient_c_runtime: tuple[_SeparateCRuntime, Path],
    debug_records: list[tuple[str, str]],
    capfd: pytest.CaptureFixture[str],
) -> None:
    runtime, terminal = mgclient_c_runtime

    def connect(**_: object) -> MagicMock:
        runtime.perror(f"{MGCLIENT_NOISE}_recv: connection closed by server")
        raise health.mgclient.OperationalError("failed to receive handshake")

    with patch.object(health.mgclient, "connect", side_effect=connect):
        assert (
            health.memgraph_anonymous_access(cs.LOOPBACK_HOST, 7687)
            is cs.AnonymousAccess.NO_ANSWER
        )
    runtime.perror("after")

    assert terminal.read_text() == "after\n"
    assert [level for level, text in debug_records if MGCLIENT_NOISE in text] == [
        "DEBUG"
    ]
    assert set(runtime.fds) == {STDERR_FD}
    assert capfd.readouterr().err == ""


def test_an_unexpected_probe_error_restores_both_c_runtimes_stderr(
    mgclient_c_runtime: tuple[_SeparateCRuntime, Path],
    capfd: pytest.CaptureFixture[str],
) -> None:
    runtime, terminal = mgclient_c_runtime

    def connect(**_: object) -> MagicMock:
        runtime.perror(MGCLIENT_NOISE)
        os.write(STDERR_FD, f"{MGCLIENT_NOISE}\n".encode())
        raise RuntimeError("bug")

    with (
        patch.object(health.mgclient, "connect", side_effect=connect),
        pytest.raises(RuntimeError, match="bug"),
    ):
        health._bolt_reachable(cs.LOOPBACK_HOST, 7687)
    runtime.perror("after")
    os.write(STDERR_FD, b"after\n")

    assert terminal.read_text() == "after\n"
    assert set(runtime.fds) == {STDERR_FD}
    assert capfd.readouterr().err == "after\n"


def test_a_c_runtime_without_stderr_still_probes(
    debug_records: list[tuple[str, str]],
    capfd: pytest.CaptureFixture[str],
) -> None:
    # msvcrt.dll with no usable fd 2, as under a test runner whose capture
    # closed the handle it shared: mgclient's message still reaches the debug
    # log, and Python's fd 2 is still kept clean.
    runtime = _SeparateCRuntime(stderr=None)

    def connect(**_: object) -> MagicMock:
        runtime.perror(f"{MGCLIENT_NOISE}_recv: connection closed by server")
        os.write(STDERR_FD, f"{MGCLIENT_NOISE}: Connection reset by peer\n".encode())
        raise health.mgclient.OperationalError("failed to receive handshake")

    with (
        patch.object(health, "_mgclient_own_c_runtime", return_value=runtime),
        patch.object(health.mgclient, "connect", side_effect=connect),
    ):
        assert (
            health.memgraph_anonymous_access(cs.LOOPBACK_HOST, 7687)
            is cs.AnonymousAccess.NO_ANSWER
        )

    assert runtime.fds == {}
    assert [level for level, text in debug_records if MGCLIENT_NOISE in text] == [
        "DEBUG"
    ]
    assert capfd.readouterr().err == ""


def test_an_unusable_fd_2_that_refuses_the_capture_is_left_as_it_was(
    tmp_path: Path,
) -> None:
    # msvcrt.dll's fd 2 still taken, but by a handle it can no longer dup:
    # when it will not take the capture either, the probe still runs and
    # reports its result, and nothing is closed after.
    with (tmp_path / "terminal").open("wb") as file:
        runtime = _SeparateCRuntime(file.fileno())
    failure = health.mgclient.OperationalError("failed to receive handshake")
    try:
        with (
            patch.object(health, "_mgclient_own_c_runtime", return_value=runtime),
            patch.object(runtime, "dup", side_effect=OSError(errno.EBADF, "gone")),
            patch.object(runtime, "dup2", side_effect=OSError(errno.EBADF, "bad")),
            patch.object(health.mgclient, "connect", side_effect=failure),
        ):
            assert (
                health.memgraph_anonymous_access(cs.LOOPBACK_HOST, 7687)
                is cs.AnonymousAccess.NO_ANSWER
            )

        assert set(runtime.fds) == {STDERR_FD}
    finally:
        for real_fd in runtime.fds.values():
            os.close(real_fd)


def test_mgclient_has_a_c_runtime_of_its_own_only_on_windows() -> None:
    # Everywhere else mgclient and Python share the kernel's descriptors.
    own_runtime = health._mgclient_own_c_runtime()

    assert (own_runtime is not None) == (sys.platform == "win32")


def test_a_failed_c_runtime_call_raises_instead_of_returning_minus_one() -> None:
    # The C runtime signals failure with -1 rather than raising, so a swap on
    # a descriptor it refused must stop before stderr is pointed anywhere.
    with pytest.raises(OSError, match=re.escape(cs.ERR_C_RUNTIME_CALL_FAILED)):
        health._c_runtime_result(-1)


def test_a_successful_c_runtime_call_returns_its_descriptor() -> None:
    assert health._c_runtime_result(7) == 7
