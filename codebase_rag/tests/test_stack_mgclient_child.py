"""Memgraph probes on Windows run mgclient in a child process (issue #2356).

pymgclient's Windows extension is a MinGW build: it imports perror, fprintf
and the rest of its stdio from msvcrt.dll, while Python's os module works on
the UCRT. The tests below model one Windows process with both C runtimes over
its handle table, with the semantics the runtimes' sources give them, and run
the probe in the states a real process starts in:

- through a venv's python.exe, whose launcher hands the interpreter stdout and
  stderr as two separate handles (CPython's PC/launcher2.c duplicates each);
- in a pytest-xdist worker, where execnet has already pointed the UCRT's fds 1
  and 2 at NUL (execnet's init_popen_io), closing the handles msvcrt.dll's
  fds 1 and 2 still name; the number may since have gone to another handle;
- on a console, where stdout and stderr are one handle.

Moving fd 2 inside the process loses mgclient's message in the first two and
closes a handle another descriptor still names. A probe that moves nothing
here and runs mgclient in a child, whose stderr is a pipe from the start,
does neither.
"""

from __future__ import annotations

import ctypes.util
import io
import itertools
import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, NoReturn
from unittest.mock import patch

import pytest
from loguru import logger

from codebase_rag import constants as root_cs
from codebase_rag import mgclient_probe
from codebase_rag.stack import constants as cs
from codebase_rag.stack import health
from codebase_rag.types_defs import MgclientProbeReport, MgclientProbeRequest

STDOUT_FD = 1
STDERR_FD = 2
# What mgclient prints for each failed read or write (mgtransport.c).
MGCLIENT_NOISE = "mg_raw_transport"
CLOSED_BY_SERVER = "mg_raw_transport_recv: connection closed by server\n"
RECV_FAILED = "mg_raw_transport_recv"
LOOPBACK = cs.LOOPBACK_HOST
LOGIN = ("cgr", "s3cret")
REPO_ROOT = Path(__file__).resolve().parents[2]


# --- A Windows process with two C runtimes over one handle table ---


@dataclass(eq=False)
class _Sink:
    """A kernel object written through a handle: a console, a pipe, a file."""

    is_console: bool = False
    data: bytearray = field(default_factory=bytearray)

    def write(self, data: bytes) -> None:
        self.data += data

    def text(self) -> str:
        return self.data.decode()


class _CaptureFile:
    """The probe's capture file: what the model writes through it lands in it."""

    is_console = False

    def __init__(self, file: IO[bytes]) -> None:
        self._fileno = file.fileno()

    def write(self, data: bytes) -> None:
        os.write(self._fileno, data)


type _KernelObject = _Sink | _CaptureFile


class _Process:
    """One process's handle table.

    CloseHandle frees a value, and Windows hands freed values to new handles,
    so a value a descriptor still names can come to name another object. The
    order it reuses them in is its own, so here a new handle takes a fresh
    value, and a test that wants a freed one reused says so with `open_at`.
    """

    def __init__(self) -> None:
        self.objects: dict[int, _KernelObject] = {}
        self.runtimes: list[_CRuntimeModel] = []
        self.closed_while_named: list[str] = []
        self._values = itertools.count(4, 4)

    def open(self, obj: _KernelObject) -> int:
        return self.open_at(next(self._values), obj)

    def open_at(self, value: int, obj: _KernelObject) -> int:
        assert value not in self.objects
        self.objects[value] = obj
        return value

    def duplicate(self, value: int) -> int | None:
        obj = self.objects.get(value)
        return None if obj is None else self.open(obj)

    def close(self, value: int, closer: _CRuntimeModel, fd: int) -> None:
        for runtime in self.runtimes:
            for other_fd, other_value in runtime.fds.items():
                if other_value == value and (runtime, other_fd) != (closer, fd):
                    self.closed_while_named.append(
                        f"{closer.name} fd {fd} closed handle {value}, "
                        f"which {runtime.name} fd {other_fd} names"
                    )
        self.objects.pop(value, None)

    def write(self, value: int, data: bytes) -> None:
        if (obj := self.objects.get(value)) is not None:
            obj.write(data)


class _CRuntimeModel:
    """A C runtime's descriptor table, as the UCRT and msvcrt.dll keep one.

    - fds 0 to 2 start on the standard handles themselves, from GetStdHandle,
      not on duplicates (lowio/ioinit.cpp; Wine's msvcrt_init_io);
    - `_dup2` duplicates the source's handle, then closes the target's
      (lowio/dup2.cpp; Wine's _dup2);
    - closing fd 1 or fd 2 skips CloseHandle while both name one handle, the
      only sharing either runtime allows for (lowio/close.cpp; Wine's _close).
    """

    def __init__(
        self, process: _Process, name: str, std_handles: tuple[int, int, int]
    ) -> None:
        self.process = process
        self.name = name
        self.fds: dict[int, int] = dict(enumerate(std_handles))
        self.calls: list[str] = []
        process.runtimes.append(self)

    def _new_fd(self, handle: int) -> int:
        fd = next(f for f in itertools.count() if f not in self.fds)
        self.fds[fd] = handle
        return fd

    def _close_handle(self, fd: int) -> None:
        shared = self.fds.get(STDOUT_FD) == self.fds.get(STDERR_FD)
        if fd in (STDOUT_FD, STDERR_FD) and shared:
            return
        self.process.close(self.fds[fd], self, fd)

    def dup(self, fd: int) -> int:
        self.calls.append(f"_dup({fd})")
        handle = self.process.duplicate(self.fds[fd])
        return -1 if handle is None else self._new_fd(handle)

    def dup2(self, fd: int, fd2: int) -> int:
        self.calls.append(f"_dup2({fd}, {fd2})")
        handle = self.process.duplicate(self.fds[fd])
        if handle is None:
            return -1
        if fd2 in self.fds:
            self._close_handle(fd2)
        self.fds[fd2] = handle
        return 0

    def close(self, fd: int) -> int:
        self.calls.append(f"_close({fd})")
        self._close_handle(fd)
        del self.fds[fd]
        return 0

    def open_osfhandle(self, handle: int) -> int:
        self.calls.append("_open_osfhandle")
        return self._new_fd(handle)

    def write(self, fd: int, data: bytes) -> None:
        self.process.write(self.fds[fd], data)

    def leads_to(self, fd: int) -> _KernelObject | None:
        return self.process.objects.get(self.fds[fd])


class _MsvcrtModel(_CRuntimeModel):
    """msvcrt.dll's table, with the stderr stream mgclient's fprintf uses.

    The stream holds its output in a buffer unless fd 2 was a console when it
    first wrote, and perror writes fd 2 directly, past the stream (Wine's
    msvcrt_alloc_buffer and add_std_buffer before msvcr140; the UCRT, by
    contrast, never buffers stderr).
    """

    def __init__(
        self, process: _Process, name: str, std_handles: tuple[int, int, int]
    ) -> None:
        super().__init__(process, name, std_handles)
        self._stderr_buffer = bytearray()
        self._stderr_buffered: bool | None = None

    def fprintf_stderr(self, text: str) -> None:
        if self._stderr_buffered is None:
            obj = self.leads_to(STDERR_FD)
            self._stderr_buffered = obj is None or not obj.is_console
        if self._stderr_buffered:
            self._stderr_buffer += text.encode()
        else:
            self.write(STDERR_FD, text.encode())

    def perror(self, prefix: str) -> None:
        # A failed recv sets WSAGetLastError(), not errno, so the text after
        # the prefix is whatever errno last held.
        self.write(STDERR_FD, f"{prefix}: No error\n".encode())

    def fflush(self, stream: None) -> int:
        self.calls.append("fflush")
        data, self._stderr_buffer = bytes(self._stderr_buffer), bytearray()
        if data:
            self.write(STDERR_FD, data)
        return 0


@dataclass
class _Windows:
    process: _Process
    terminal: _Sink
    ucrt: _CRuntimeModel
    msvcrt: _MsvcrtModel

    def forget_history(self) -> None:
        self.ucrt.calls.clear()
        self.msvcrt.calls.clear()
        self.process.closed_while_named.clear()


def _through_a_venv(stderr: _Sink | None = None) -> _Windows:
    """python.exe as a venv's launcher starts it: stdout and stderr separate."""
    process = _Process()
    terminal = _Sink(is_console=True)
    stdin = process.open(_Sink())
    stdout = process.open(terminal)
    std = (stdin, stdout, process.open(stderr or terminal))
    return _Windows(
        process,
        terminal,
        _CRuntimeModel(process, "ucrt", std),
        _MsvcrtModel(process, "msvcrt", std),
    )


def _on_a_console() -> _Windows:
    """A console process whose stdout and stderr are one handle."""
    process = _Process()
    terminal = _Sink(is_console=True)
    stdin = process.open(_Sink())
    out = process.open(terminal)
    std = (stdin, out, out)
    return _Windows(
        process,
        terminal,
        _CRuntimeModel(process, "ucrt", std),
        _MsvcrtModel(process, "msvcrt", std),
    )


def _bootstrap_a_test_worker(windows: _Windows) -> None:
    # execnet's init_popen_io on Windows: `os.dup2(nul, 1)`, then
    # `os.dup2(nul, 2)`, through the UCRT only.
    ucrt = windows.ucrt
    nul = ucrt.open_osfhandle(windows.process.open(_Sink()))
    ucrt.dup2(nul, STDOUT_FD)
    ucrt.dup2(nul, STDERR_FD)
    ucrt.close(nul)


def _in_a_test_worker(*, stale_number_reused: bool) -> _Windows:
    """A pytest-xdist worker after execnet's bootstrap moved fds 1 and 2."""
    windows = _through_a_venv()
    _bootstrap_a_test_worker(windows)
    if stale_number_reused:
        # Some file the worker opened since, under the number msvcrt.dll's
        # fd 2 still holds.
        stale = windows.msvcrt.fds[STDERR_FD]
        windows.ucrt.open_osfhandle(windows.process.open_at(stale, _Sink()))
    windows.forget_history()
    return windows


def _in_a_worker_whose_stale_number_is_free() -> _Windows:
    return _in_a_test_worker(stale_number_reused=False)


def _in_a_worker_whose_stale_number_was_reused() -> _Windows:
    return _in_a_test_worker(stale_number_reused=True)


STARTS = [
    pytest.param(_through_a_venv, id="venv-python"),
    pytest.param(_in_a_worker_whose_stale_number_is_free, id="xdist-worker-free"),
    pytest.param(_in_a_worker_whose_stale_number_was_reused, id="xdist-worker-reused"),
    pytest.param(_on_a_console, id="console"),
]


class _AsPythonsRuntime:
    """The model's UCRT, called the way health.py calls Python's os module."""

    def __init__(self, ucrt: _CRuntimeModel) -> None:
        self._ucrt = ucrt

    def dup(self, fd: int) -> int:
        return _succeeded(self._ucrt.dup(fd))

    def dup2(self, fd: int, fd2: int) -> None:
        _succeeded(self._ucrt.dup2(fd, fd2))

    def close(self, fd: int) -> None:
        self._ucrt.close(fd)

    def open_file(self, file: IO[bytes]) -> int:
        process = self._ucrt.process
        return self._ucrt.open_osfhandle(process.open(_CaptureFile(file)))


class _AsMgclientsRuntime(_AsPythonsRuntime):
    """The model's msvcrt.dll, handed to health.py as mgclient's own runtime.

    It can be driven the way a redirect inside the process would drive it:
    flush the stdio, then `_dup2`. A probe that did would be seen here.
    """

    def __init__(self, msvcrt: _MsvcrtModel) -> None:
        super().__init__(msvcrt)
        self._msvcrt = msvcrt
        self.name = cs.MGCLIENT_WINDOWS_C_RUNTIME

    def dup2(self, fd: int, fd2: int) -> None:
        self._msvcrt.fflush(None)
        super().dup2(fd, fd2)


def _succeeded(result: int) -> int:
    if result == -1:
        raise OSError(result, "the C runtime refused the call")
    return result


type _Printer = Callable[[_MsvcrtModel], Callable[..., NoReturn]]


def _closed_by_server(msvcrt: _MsvcrtModel) -> Callable[..., NoReturn]:
    # mg_raw_transport_recv on a recv that returned 0.
    def connect(**_: object) -> NoReturn:
        msvcrt.fprintf_stderr(CLOSED_BY_SERVER)
        raise health.mgclient.OperationalError("failed to receive handshake response")

    return connect


def _reset_by_server(msvcrt: _MsvcrtModel) -> Callable[..., NoReturn]:
    # mg_raw_transport_recv on a recv that failed.
    def connect(**_: object) -> NoReturn:
        msvcrt.perror(RECV_FAILED)
        raise health.mgclient.OperationalError("failed to receive handshake response")

    return connect


PRINTERS = [
    pytest.param(_closed_by_server, id="close"),
    pytest.param(_reset_by_server, id="reset"),
]


@dataclass
class _ChildRun:
    """subprocess.run, standing in for the probe's child process.

    The child is a fresh process started through a venv's launcher, with a
    pipe as stderr; in it the real mgclient_probe.main runs, against a
    connect that prints the way `printer` does.
    """

    printer: _Printer
    children: list[_Windows] = field(default_factory=list)

    def __call__(
        self, args: list[str], *, input: str, **_: object
    ) -> subprocess.CompletedProcess[str]:
        pipe = _Sink()
        child = _through_a_venv(stderr=pipe)
        self.children.append(child)
        stdout = io.StringIO()

        def load(name: str) -> _MsvcrtModel:
            assert name == cs.MGCLIENT_WINDOWS_C_RUNTIME
            return child.msvcrt

        with (
            patch.object(
                mgclient_probe.mgclient, "connect", self.printer(child.msvcrt)
            ),
            patch.object(mgclient_probe.ctypes, "CDLL", side_effect=load),
            patch.object(sys, "stdin", io.StringIO(input)),
            patch.object(sys, "stdout", stdout),
        ):
            code = mgclient_probe.main()
        return subprocess.CompletedProcess(args, code, stdout.getvalue(), pipe.text())


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


def _probe(
    windows: _Windows, printer: _Printer, monkeypatch: pytest.MonkeyPatch
) -> cs.AnonymousAccess:
    monkeypatch.setattr(health, "_PYTHON_C_RUNTIME", _AsPythonsRuntime(windows.ucrt))
    mgclients_runtime = _AsMgclientsRuntime(windows.msvcrt)
    monkeypatch.setattr(health, "_mgclient_own_c_runtime", lambda: mgclients_runtime)
    monkeypatch.setattr(health.mgclient, "connect", printer(windows.msvcrt))
    monkeypatch.setattr(subprocess, "run", _ChildRun(printer))
    return health.memgraph_anonymous_access(LOOPBACK, 7687)


def _noise(records: list[tuple[str, str]]) -> list[str]:
    return [level for level, text in records if MGCLIENT_NOISE in text]


# --- The states themselves ---


def test_a_test_worker_starts_with_msvcrt_naming_handles_execnet_closed() -> None:
    # Why the in-process test was red only sometimes on Windows CI: in every
    # worker msvcrt.dll's fd 2 names a handle the UCRT closed at bootstrap.
    # While that number is free, moving it fails and mgclient's message is
    # lost; once a new handle reuses it, the move closes that handle instead.
    windows = _through_a_venv()
    stderr = windows.msvcrt.fds[STDERR_FD]

    _bootstrap_a_test_worker(windows)

    assert windows.process.closed_while_named == [
        "ucrt fd 1 closed handle 8, which msvcrt fd 1 names",
        "ucrt fd 2 closed handle 12, which msvcrt fd 2 names",
    ]
    assert windows.msvcrt.fds[STDERR_FD] == stderr
    assert windows.msvcrt.leads_to(STDERR_FD) is None


def test_on_a_console_moving_fd_2_closes_nothing_the_other_runtime_names() -> None:
    # The one start the move inside the process was safe in: stdout and
    # stderr share a handle, so neither runtime closes it.
    windows = _on_a_console()
    capture = windows.ucrt.open_osfhandle(windows.process.open(_Sink()))

    windows.ucrt.dup2(capture, STDERR_FD)

    assert windows.process.closed_while_named == []
    assert windows.msvcrt.leads_to(STDERR_FD) is windows.terminal


# --- The probe in each state a Windows process starts in ---


@pytest.mark.parametrize("printer", PRINTERS)
@pytest.mark.parametrize("start", STARTS)
def test_what_mgclient_prints_is_logged_once_at_debug(
    start: Callable[[], _Windows],
    printer: _Printer,
    debug_records: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access = _probe(start(), printer, monkeypatch)

    assert access is cs.AnonymousAccess.NO_ANSWER
    assert _noise(debug_records) == ["DEBUG"]


@pytest.mark.parametrize("start", STARTS)
def test_a_probe_closes_no_handle_another_descriptor_names(
    start: Callable[[], _Windows], monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = start()

    _probe(windows, _closed_by_server, monkeypatch)

    assert windows.process.closed_while_named == []


@pytest.mark.parametrize(
    "start",
    [
        pytest.param(_through_a_venv, id="venv-python"),
        pytest.param(_on_a_console, id="console"),
    ],
)
def test_after_a_probe_both_runtimes_still_print_to_the_terminal(
    start: Callable[[], _Windows], monkeypatch: pytest.MonkeyPatch
) -> None:
    windows = start()

    _probe(windows, _closed_by_server, monkeypatch)
    windows.msvcrt.perror("later")
    windows.ucrt.write(STDERR_FD, b"python\n")

    assert windows.terminal.text() == "later: No error\npython\n"


def test_a_probe_leaves_both_c_runtimes_descriptors_alone(
    debug_records: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even on a console, where stdout and stderr share the handle and moving
    # fd 2 closes nothing, no order of moves is safe in the other starts.
    windows = _on_a_console()

    _probe(windows, _reset_by_server, monkeypatch)

    assert windows.ucrt.calls == []
    assert windows.msvcrt.calls == []
    assert _noise(debug_records) == ["DEBUG"]


# --- The child: what it runs, and what it hands back ---


@pytest.mark.parametrize("printer", PRINTERS)
def test_the_probe_child_sends_what_mgclient_printed_down_its_stderr(
    printer: _Printer,
) -> None:
    # fprintf(stderr) is buffered while fd 2 is a pipe; without the flush
    # the "close" text would still be in msvcrt.dll when the child exits.
    child_run = _ChildRun(printer)
    request = _request(c_runtime=cs.MGCLIENT_WINDOWS_C_RUNTIME)

    child = child_run(["probe"], input=json.dumps(request))

    assert MGCLIENT_NOISE in child.stderr
    assert json.loads(child.stdout) == MgclientProbeReport(
        succeeded=False, connect_error="failed to receive handshake response"
    )
    assert child_run.children[0].msvcrt.calls == ["fflush"]


def test_the_probe_child_flushes_even_when_the_session_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pipe = _Sink()
    child = _through_a_venv(stderr=pipe)

    def connect(**_: object) -> NoReturn:
        child.msvcrt.fprintf_stderr(CLOSED_BY_SERVER)
        raise RuntimeError("bug")

    request = _request(c_runtime=cs.MGCLIENT_WINDOWS_C_RUNTIME)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(mgclient_probe.mgclient, "connect", connect)
    monkeypatch.setattr(mgclient_probe.ctypes, "CDLL", lambda _: child.msvcrt)

    with pytest.raises(RuntimeError, match="bug"):
        mgclient_probe.main()

    assert pipe.text() == CLOSED_BY_SERVER


def _request(
    *,
    credentials: list[str] | None = None,
    query: str | None = cs.BOLT_PROBE_QUERY,
    c_runtime: str = cs.MGCLIENT_WINDOWS_C_RUNTIME,
) -> MgclientProbeRequest:
    return MgclientProbeRequest(
        host=LOOPBACK,
        port=7687,
        credentials=credentials,
        query=query,
        c_runtime=c_runtime,
    )


@pytest.mark.parametrize(
    ("credentials", "expected_call"),
    [
        (None, {"host": LOOPBACK, "port": 7687}),
        (
            list(LOGIN),
            {"host": LOOPBACK, "port": 7687, "username": "cgr", "password": "s3cret"},
        ),
    ],
)
def test_the_probe_child_connects_as_the_in_process_probe_does(
    credentials: list[str] | None, expected_call: dict[str, object]
) -> None:
    with patch.object(mgclient_probe.mgclient, "connect") as connect:
        report = mgclient_probe.run(_request(credentials=credentials))

    assert report == MgclientProbeReport(succeeded=True, connect_error=None)
    connect.assert_called_once_with(**expected_call)
    cursor = connect.return_value.cursor.return_value
    cursor.execute.assert_called_once_with(cs.BOLT_PROBE_QUERY)
    cursor.fetchall.assert_called_once_with()
    connect.return_value.close.assert_called_once_with()


def test_the_probe_child_without_a_query_only_connects_and_closes() -> None:
    with patch.object(mgclient_probe.mgclient, "connect") as connect:
        report = mgclient_probe.run(_request(credentials=list(LOGIN), query=None))

    assert report == MgclientProbeReport(succeeded=True, connect_error=None)
    connect.return_value.cursor.assert_not_called()
    connect.return_value.close.assert_called_once_with()


@pytest.mark.parametrize(
    ("failure", "connect_error"),
    [
        (
            health.mgclient.OperationalError("Authentication failure"),
            "Authentication failure",
        ),
        (OSError("unreachable"), None),
    ],
)
def test_the_probe_child_reports_how_connect_failed(
    failure: Exception, connect_error: str | None
) -> None:
    with patch.object(mgclient_probe.mgclient, "connect", side_effect=failure):
        report = mgclient_probe.run(_request())

    assert report == MgclientProbeReport(succeeded=False, connect_error=connect_error)


def test_a_failed_query_is_a_failure_and_still_closes() -> None:
    with patch.object(mgclient_probe.mgclient, "connect") as connect:
        connect.return_value.cursor.return_value.execute.side_effect = (
            health.mgclient.DatabaseError("Authentication failure")
        )
        report = mgclient_probe.run(_request())

    # Only connect's error can say a login was refused.
    assert report == MgclientProbeReport(succeeded=False, connect_error=None)
    connect.return_value.close.assert_called_once_with()


# --- The parent: how a probe reaches its child, and reads it back ---


@dataclass
class _RecordedRun:
    """subprocess.run returning a fixed child result, recording each call."""

    result: subprocess.CompletedProcess[str] | subprocess.TimeoutExpired
    calls: list[tuple[list[str], dict[str, object]]] = field(default_factory=list)

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        if isinstance(self.result, subprocess.TimeoutExpired):
            raise self.result
        return self.result


def _child_reporting(
    report: MgclientProbeReport, stderr: str = "", code: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, json.dumps(report), stderr)


@pytest.fixture
def probes_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = health._CRuntimeLibrary(cs.MGCLIENT_WINDOWS_C_RUNTIME)
    monkeypatch.setattr(health, "_mgclient_own_c_runtime", lambda: runtime)


@pytest.mark.usefixtures("probes_on_windows")
def test_the_login_reaches_the_child_on_stdin_not_its_command_line() -> None:
    run = _RecordedRun(
        _child_reporting(MgclientProbeReport(succeeded=True, connect_error=None))
    )
    with patch.object(subprocess, "run", run):
        assert health._bolt_reachable(LOOPBACK, 7687, LOGIN)

    [(args, kwargs)] = run.calls
    assert not any(secret in arg for arg in args for secret in LOGIN)
    request = json.loads(str(kwargs["input"]))
    assert request == _request(credentials=list(LOGIN))
    assert kwargs["encoding"] == root_cs.ENCODING_UTF8


@pytest.mark.usefixtures("probes_on_windows")
@pytest.mark.parametrize(
    ("probe", "credentials", "query"),
    [
        pytest.param(
            lambda: health.memgraph_anonymous_access(LOOPBACK, 7687),
            None,
            cs.BOLT_PROBE_QUERY,
            id="anonymous-access",
        ),
        pytest.param(
            lambda: health.memgraph_rejects_credentials(LOOPBACK, 7687, LOGIN),
            list(LOGIN),
            None,
            id="rejects-credentials",
        ),
    ],
)
def test_each_probe_asks_its_child_for_the_session_it_runs_in_process(
    probe: Callable[[], object], credentials: list[str] | None, query: str | None
) -> None:
    run = _RecordedRun(
        _child_reporting(MgclientProbeReport(succeeded=True, connect_error=None))
    )
    with patch.object(subprocess, "run", run):
        probe()

    [(_, kwargs)] = run.calls
    assert json.loads(str(kwargs["input"])) == _request(
        credentials=credentials, query=query
    )


@pytest.mark.usefixtures("probes_on_windows")
@pytest.mark.parametrize(
    ("report", "reachable", "access", "rejected"),
    [
        (
            MgclientProbeReport(succeeded=True, connect_error=None),
            True,
            cs.AnonymousAccess.ALLOWED,
            False,
        ),
        (
            MgclientProbeReport(
                succeeded=False, connect_error="Authentication failure"
            ),
            False,
            cs.AnonymousAccess.REFUSED,
            True,
        ),
        (
            MgclientProbeReport(
                succeeded=False,
                connect_error="couldn't connect to host: Connection refused",
            ),
            False,
            cs.AnonymousAccess.NO_ANSWER,
            False,
        ),
        (
            MgclientProbeReport(succeeded=False, connect_error=None),
            False,
            cs.AnonymousAccess.NO_ANSWER,
            False,
        ),
    ],
)
def test_a_child_report_answers_each_probe_as_the_in_process_probe_would(
    report: MgclientProbeReport,
    reachable: bool,
    access: cs.AnonymousAccess,
    rejected: bool,
) -> None:
    with patch.object(subprocess, "run", _RecordedRun(_child_reporting(report))):
        assert health._bolt_reachable(LOOPBACK, 7687, LOGIN) is reachable
        assert health.memgraph_anonymous_access(LOOPBACK, 7687) is access
        assert health.memgraph_rejects_credentials(LOOPBACK, 7687, LOGIN) is rejected


@pytest.mark.usefixtures("probes_on_windows")
def test_a_child_that_fails_raises_rather_than_passing_for_no_answer() -> None:
    # NO_ANSWER would let an open Memgraph through the anonymous-access check.
    crashed = subprocess.CompletedProcess([], 1, "", "Traceback: ImportError\n")
    with (
        patch.object(subprocess, "run", _RecordedRun(crashed)),
        pytest.raises(ChildProcessError, match="exit code 1: Traceback: ImportError"),
    ):
        health.memgraph_anonymous_access(LOOPBACK, 7687)


@pytest.mark.usefixtures("probes_on_windows")
def test_a_child_that_never_answers_is_no_answer(
    debug_records: list[tuple[str, str]],
) -> None:
    hung = subprocess.TimeoutExpired([], cs.MGCLIENT_PROBE_TIMEOUT_S)
    with patch.object(subprocess, "run", _RecordedRun(hung)) as run:
        assert (
            health.memgraph_anonymous_access(LOOPBACK, 7687)
            is cs.AnonymousAccess.NO_ANSWER
        )

    [(_, kwargs)] = run.calls
    assert kwargs["timeout"] == cs.MGCLIENT_PROBE_TIMEOUT_S
    assert debug_records == [
        (
            "DEBUG",
            cs.MSG_MEMGRAPH_PROBE_TIMED_OUT.format(timeout=cs.MGCLIENT_PROBE_TIMEOUT_S),
        )
    ]


def test_the_child_runs_the_probe_module_with_the_working_directory_off_its_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sys, cs.FROZEN_APP_ATTR, raising=False)

    assert health._mgclient_probe_command() == [
        sys.executable,
        "-P",
        "-m",
        "codebase_rag.mgclient_probe",
    ]


def test_a_frozen_build_runs_itself_as_the_probe_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, cs.FROZEN_APP_ATTR, True, raising=False)

    assert health._mgclient_probe_command() == [
        sys.executable,
        root_cs.MGCLIENT_PROBE_CHILD_ARG,
    ]


# --- Real processes ---


def _host_c_runtime() -> str:
    # msvcrt.dll on Windows; elsewhere this platform's C library, whose
    # fflush(NULL) the child calls in its place.
    if (runtime := health._mgclient_own_c_runtime()) is not None:
        return runtime.name
    return str(ctypes.util.find_library("c"))


def _closed_port() -> int:
    with socket.create_server((LOOPBACK, 0)) as listener:
        return listener.getsockname()[1]


def test_the_frozen_entry_point_answers_the_probe_argument_with_a_report() -> None:
    # main.py is what the frozen binary runs; nothing listens on the port.
    request = _request(c_runtime=_host_c_runtime()) | {"port": _closed_port()}
    child = subprocess.run(
        [sys.executable, str(REPO_ROOT / "main.py"), root_cs.MGCLIENT_PROBE_CHILD_ARG],
        input=json.dumps(request),
        capture_output=True,
        encoding=root_cs.ENCODING_UTF8,
        timeout=cs.MGCLIENT_PROBE_TIMEOUT_S,
        check=False,
        cwd=REPO_ROOT,
    )

    assert child.returncode == 0, child.stderr
    report = json.loads(child.stdout)
    assert report["succeeded"] is False
    assert "couldn't connect to host" in report["connect_error"]


# A Bolt server that refuses every login, as Memgraph does without the right
# credentials. In its own process: mgclient holds the GIL while it waits.
_REFUSING_MEMGRAPH = r"""
import socket, struct

def text(value):
    data = value.encode()
    return (bytes([0x80 | len(data)]) if len(data) < 16 else bytes([0xD0, len(data)])) + data

def read(conn, size):
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data

FAILURE = bytes([0xB1, 0x7F, 0xA2]) + text("code") + text(
    "Memgraph.ClientError.Security.Unauthenticated"
) + text("message") + text("Authentication failure")
listener = socket.create_server(("127.0.0.1", 0))
print(listener.getsockname()[1], flush=True)
while True:
    conn, _ = listener.accept()
    try:
        read(conn, 20)
        conn.sendall(bytes([0, 0, 1, 4]))
        while size := struct.unpack(">H", read(conn, 2))[0]:
            read(conn, size)
        conn.sendall(struct.pack(">H", len(FAILURE)) + FAILURE + b"\x00\x00")
        conn.recv(1024)
    except (EOFError, OSError):
        pass
    conn.close()
"""


@pytest.fixture
def refusing_memgraph() -> Iterator[int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _REFUSING_MEMGRAPH],
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


@pytest.mark.parametrize("where", ["this-process", "child"])
def test_a_refused_login_is_told_apart_from_no_answer_wherever_the_probe_runs(
    where: str, refusing_memgraph: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = (
        None if where == "this-process" else health._CRuntimeLibrary(_host_c_runtime())
    )
    monkeypatch.setattr(health, "_mgclient_own_c_runtime", lambda: runtime)

    assert (
        health.memgraph_anonymous_access(LOOPBACK, refusing_memgraph)
        is cs.AnonymousAccess.REFUSED
    )
    assert health.memgraph_rejects_credentials(LOOPBACK, refusing_memgraph, LOGIN)
    assert not health._bolt_reachable(LOOPBACK, refusing_memgraph, LOGIN)
