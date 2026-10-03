from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.client import HTTPMessage
from typing import IO, NamedTuple, Protocol

import mgclient
from loguru import logger

from .. import constants as root_cs
from .. import mgclient_probe
from ..types_defs import MgclientProbeReport, MgclientProbeRequest
from . import constants as cs

# pymgclient 1.6 re-exports its C extension through `import *`, which a type
# checker cannot see into, so the exception type is bound once here.
_MgclientError: type[Exception] = mgclient.Error  # ty: ignore[unresolved-attribute]


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


# The Qdrant probes can carry the API key, and urllib's default opener would
# route even a loopback request through an HTTP_PROXY that no_proxy does not
# exempt, handing the key to the proxy. Its redirect handler would likewise
# copy the key onto a redirect to any host; Qdrant never redirects these
# endpoints, so a 3xx fails the probe instead of being followed.
_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _RefuseRedirects()
)


# mgclient's C transport reports each failed read or write with perror() on
# fd 2, beneath sys.stderr, although the probe gets the same failure back as
# the exception it handles. While Memgraph starts, Docker's port proxy accepts
# every probe and then drops it, so waiting printed one such line per attempt
# (issue #2356). Only the descriptor itself can be pointed elsewhere, and it is
# one per process, so probes take turns: two swapping it at once would each
# restore the other's capture file as stderr.
_NATIVE_STDERR_LOCK = threading.Lock()


class _CRuntime(Protocol):
    # A C runtime's descriptor table. POSIX has one per process, the kernel's;
    # on Windows each C runtime DLL keeps its own, so fd 2 in one is not fd 2
    # in another, though both start out naming the same OS handle.
    def dup(self, fd: int) -> int: ...

    def dup2(self, fd: int, fd2: int) -> None: ...

    def close(self, fd: int) -> None: ...

    def open_file(self, file: IO[bytes]) -> int: ...


class _PythonCRuntime:
    # The one behind Python's os module.
    def dup(self, fd: int) -> int:
        return os.dup(fd)

    def dup2(self, fd: int, fd2: int) -> None:
        os.dup2(fd, fd2)

    def close(self, fd: int) -> None:
        os.close(fd)

    def open_file(self, file: IO[bytes]) -> int:
        return os.dup(file.fileno())


_PYTHON_C_RUNTIME = _PythonCRuntime()


class _CRuntimeLibrary(NamedTuple):
    # A C runtime by the name its library loads under.
    name: str


def _mgclient_own_c_runtime() -> _CRuntimeLibrary | None:
    """The C runtime besides Python's that mgclient prints through, if any.

    pymgclient's Windows extension imports perror, fprintf and its other stdio
    from msvcrt.dll, not the UCRT. Neither runtime duplicates the process's
    standard handles when it starts, so their fd 2 name one OS handle, and
    each runtime's `_dup2` onto fd 2 closes the handle fd 2 named, unless fd 1
    names the same one (lowio/close.cpp in the UCRT sources). Moving fd 2 in
    both runtimes therefore closes that handle twice: the second close finds
    it gone, or closes whatever has since been given its number. A worker
    process that pytest or execnet already moved fd 2 in starts the same way,
    with msvcrt.dll's fd 2 naming a closed handle. No order of the two moves
    avoids that, so probes on Windows run mgclient in a child instead.
    """
    if sys.platform != "win32":
        return None
    return _CRuntimeLibrary(cs.MGCLIENT_WINDOWS_C_RUNTIME)


def _log_mgclient_output(output: str) -> None:
    # Kept rather than dropped, for whoever is chasing a Memgraph that never
    # answers; the probe's own result is what reports it.
    if output := output.strip():
        logger.debug(cs.MSG_MEMGRAPH_PROBE_OUTPUT.format(output=output))


def _mgclient_probe_command() -> list[str]:
    if getattr(sys, cs.FROZEN_APP_ATTR, False):
        return [sys.executable, root_cs.MGCLIENT_PROBE_CHILD_ARG]
    # -P keeps the working directory off the child's sys.path, so a
    # codebase_rag checkout there cannot stand in for the installed package.
    return [
        sys.executable,
        cs.PYTHON_SAFE_PATH_FLAG,
        cs.PYTHON_RUN_MODULE_FLAG,
        mgclient_probe.__name__,
    ]


def _mgclient_session_in_child(
    runtime: _CRuntimeLibrary,
    host: str,
    port: int,
    credentials: tuple[str, str] | None,
    query: str | None,
) -> MgclientProbeReport:
    # The child's stderr is a pipe from the moment it starts, so neither
    # process moves a descriptor; the login travels on stdin, not argv.
    request = MgclientProbeRequest(
        host=host,
        port=port,
        credentials=None if credentials is None else list(credentials),
        query=query,
        c_runtime=runtime.name,
    )
    try:
        child = subprocess.run(
            _mgclient_probe_command(),
            input=json.dumps(request),
            capture_output=True,
            encoding=root_cs.ENCODING_UTF8,
            errors="replace",
            timeout=cs.MGCLIENT_PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        # run() has killed and reaped the child before raising. What it means
        # depends on the probe, so each caller decides.
        _log_mgclient_output(_printed_before_the_timeout(e))
        logger.debug(
            cs.MSG_MEMGRAPH_PROBE_TIMED_OUT.format(timeout=cs.MGCLIENT_PROBE_TIMEOUT_S)
        )
        raise
    if child.returncode != 0:
        # Not reported as no answer: the anonymous-access probe guards the
        # stack's authentication, and a probe that could not run must not
        # pass for a Memgraph that refused it.
        raise ChildProcessError(
            cs.ERR_MGCLIENT_PROBE_CHILD_FAILED.format(
                code=child.returncode, output=child.stderr.strip()
            )
        )
    _log_mgclient_output(child.stderr)
    return json.loads(child.stdout)


def _printed_before_the_timeout(error: subprocess.TimeoutExpired) -> str:
    # Text on Windows, where run() reads the pipes to their end after the
    # kill; elsewhere the bytes read before the timeout, if any.
    printed = error.stderr
    if isinstance(printed, bytes):
        return printed.decode(root_cs.ENCODING_UTF8, errors="replace")
    return printed or ""


def _login_refused(report: MgclientProbeReport) -> bool:
    # Memgraph refuses a login at connect, with the same exception type as a
    # refused connection, so only the message tells the two apart.
    error = report["connect_error"]
    return error is not None and cs.MEMGRAPH_AUTH_FAILURE in error


@contextmanager
def _stderr_into(runtime: _CRuntime, capture: IO[bytes]) -> Iterator[None]:
    try:
        saved = runtime.dup(cs.NATIVE_STDERR_FD)
    except OSError:
        # With fd 2 closed there is no terminal to keep clean.
        yield
        return
    try:
        target = runtime.open_file(capture)
        try:
            runtime.dup2(target, cs.NATIVE_STDERR_FD)
        finally:
            runtime.close(target)
        yield
    finally:
        runtime.dup2(saved, cs.NATIVE_STDERR_FD)
        runtime.close(saved)


@contextmanager
def _mgclient_stderr_to_debug_log() -> Iterator[None]:
    with _NATIVE_STDERR_LOCK, tempfile.TemporaryFile() as capture:
        # Text Python still buffers for the terminal belongs there, not in
        # the capture.
        if sys.stderr is not None:
            sys.stderr.flush()
        try:
            with _stderr_into(_PYTHON_C_RUNTIME, capture):
                yield
        finally:
            capture.seek(0)
            _log_mgclient_output(
                capture.read().decode(root_cs.ENCODING_UTF8, errors="replace")
            )


def _bolt_reachable(
    host: str, port: int, credentials: tuple[str, str] | None = None
) -> bool:
    if (runtime := _mgclient_own_c_runtime()) is None:
        return _bolt_reachable_in_process(host, port, credentials)
    try:
        report = _mgclient_session_in_child(
            runtime, host, port, credentials, cs.BOLT_PROBE_QUERY
        )
    except subprocess.TimeoutExpired:
        # Not reachable yet, like a refused connection: the wait asks again.
        return False
    return report["succeeded"]


@_mgclient_stderr_to_debug_log()
def _bolt_reachable_in_process(
    host: str, port: int, credentials: tuple[str, str] | None
) -> bool:
    try:
        if credentials:
            username, password = credentials
            conn = mgclient.connect(
                host=host, port=port, username=username, password=password
            )
        else:
            conn = mgclient.connect(host=host, port=port)
        try:
            cursor = conn.cursor()
            cursor.execute(cs.BOLT_PROBE_QUERY)
            cursor.fetchall()
        finally:
            conn.close()
        return True
    except (_MgclientError, OSError):
        return False


def _http_reachable(url: str, timeout: float = 1.5) -> bool:
    # Direct, like the data probes: through a proxy, a refusal could fail a
    # running stack, and a proxy's own error page could pass a stopped one.
    try:
        with _DIRECT_OPENER.open(url, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except OSError:
        return False


def wait_for_memgraph(
    host: str,
    port: int,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
    credentials: tuple[str, str] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _bolt_reachable(host, port, credentials):
            return True
        time.sleep(interval)
    return False


def memgraph_accepts_anonymous(host: str, port: int) -> bool:
    return memgraph_anonymous_access(host, port) is cs.AnonymousAccess.ALLOWED


def memgraph_anonymous_access(host: str, port: int) -> cs.AnonymousAccess:
    if (runtime := _mgclient_own_c_runtime()) is None:
        return _anonymous_access_in_process(host, port)
    try:
        report = _mgclient_session_in_child(
            runtime, host, port, None, cs.BOLT_PROBE_QUERY
        )
    except subprocess.TimeoutExpired as e:
        # A Memgraph that took the connection and never answered may still
        # accept anonymous logins. Like a child that failed, this has no
        # answer to give the check that the stack enforces its credentials.
        raise ChildProcessError(
            cs.ERR_MGCLIENT_PROBE_TIMED_OUT.format(timeout=cs.MGCLIENT_PROBE_TIMEOUT_S)
        ) from e
    if report["succeeded"]:
        return cs.AnonymousAccess.ALLOWED
    if _login_refused(report):
        return cs.AnonymousAccess.REFUSED
    return cs.AnonymousAccess.NO_ANSWER


@_mgclient_stderr_to_debug_log()
def _anonymous_access_in_process(host: str, port: int) -> cs.AnonymousAccess:
    # Memgraph refuses a login at connect, with the same exception type as a
    # refused connection, so only the message tells the two apart.
    try:
        conn = mgclient.connect(host=host, port=port)
    except _MgclientError as e:
        if cs.MEMGRAPH_AUTH_FAILURE in str(e):
            return cs.AnonymousAccess.REFUSED
        return cs.AnonymousAccess.NO_ANSWER
    except OSError:
        return cs.AnonymousAccess.NO_ANSWER
    try:
        cursor = conn.cursor()
        cursor.execute(cs.BOLT_PROBE_QUERY)
        cursor.fetchall()
    except (_MgclientError, OSError):
        return cs.AnonymousAccess.NO_ANSWER
    finally:
        conn.close()
    return cs.AnonymousAccess.ALLOWED


def memgraph_rejects_credentials(
    host: str, port: int, credentials: tuple[str, str]
) -> bool:
    if (runtime := _mgclient_own_c_runtime()) is None:
        return _rejects_credentials_in_process(host, port, credentials)
    try:
        report = _mgclient_session_in_child(runtime, host, port, credentials, None)
    except subprocess.TimeoutExpired:
        # No answer is no refusal, and this probe only reports a refusal.
        return False
    return _login_refused(report)


@_mgclient_stderr_to_debug_log()
def _rejects_credentials_in_process(
    host: str, port: int, credentials: tuple[str, str]
) -> bool:
    username, password = credentials
    try:
        conn = mgclient.connect(
            host=host, port=port, username=username, password=password
        )
    except _MgclientError as e:
        return cs.MEMGRAPH_AUTH_FAILURE in str(e)
    except OSError:
        return False
    conn.close()
    return False


def qdrant_accepts_anonymous(
    port: int, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> bool:
    request = urllib.request.Request(_qdrant_url(host, port, cs.QDRANT_DATA_PROBE_PATH))  # noqa: S310 - _qdrant_url fixes the http scheme
    return _qdrant_answers(request, timeout)


def qdrant_anonymous_access(
    port: int, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> cs.AnonymousAccess:
    request = urllib.request.Request(_qdrant_url(host, port, cs.QDRANT_DATA_PROBE_PATH))  # noqa: S310 - _qdrant_url fixes the http scheme
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except OSError:
        return cs.AnonymousAccess.NO_ANSWER
    if status == 200:
        return cs.AnonymousAccess.ALLOWED
    if status in cs.HTTP_AUTH_REFUSED_STATUSES:
        return cs.AnonymousAccess.REFUSED
    return cs.AnonymousAccess.NO_ANSWER


def qdrant_accepts_key(
    port: int, api_key: str, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> bool:
    """Whether the key lets the app write, as indexing needs.

    A read-only key (QDRANT__SERVICE__READ_ONLY_API_KEY) may list collections
    too, so the probe is an alias update with no actions: Qdrant requires
    write access for it, and it changes nothing.
    """
    request = urllib.request.Request(  # noqa: S310 - _qdrant_url fixes the http scheme
        _qdrant_url(host, port, cs.QDRANT_WRITE_PROBE_PATH),
        data=cs.QDRANT_WRITE_PROBE_BODY,
        headers={
            cs.QDRANT_API_KEY_HEADER: api_key,
            cs.HTTP_CONTENT_TYPE_HEADER: cs.JSON_CONTENT_TYPE,
        },
        method=cs.HTTP_METHOD_POST,
    )
    return _qdrant_answers(request, timeout)


def qdrant_identifies(
    port: int, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> bool:
    """Whether the server answers Qdrant's root as Qdrant does.

    A bare 200 proves nothing about who is listening: any local service can
    give one. Qdrant's root names it and its version, which another service
    has no reason to imitate.
    """
    request = urllib.request.Request(_qdrant_url(host, port, cs.QDRANT_ROOT_PATH))  # noqa: S310 - _qdrant_url fixes the http scheme
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read(cs.QDRANT_ROOT_MAX_BYTES))
    except (OSError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    title = body.get(cs.QDRANT_ROOT_TITLE_KEY)
    version = body.get(cs.QDRANT_ROOT_VERSION_KEY)
    return (
        isinstance(title, str)
        and cs.QDRANT_ROOT_TITLE_MARKER in title.lower()
        and isinstance(version, str)
        and bool(version)
    )


def qdrant_base_url(host: str, port: int) -> str:
    return _qdrant_url(host, port, "")


def _qdrant_url(host: str, port: int, path: str) -> str:
    # An IPv6 address needs brackets in a URL. Plain http because the bundled
    # Qdrant serves nothing else: it runs on this machine with no TLS. The
    # keyed probe sends its key over it only with QDRANT_ALLOW_INSECURE_API_KEY,
    # the rule the app applies to an http:// QDRANT_URL (python:S5332 accepted).
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}{path}"  # NOSONAR


def _qdrant_answers(request: urllib.request.Request, timeout: float) -> bool:
    try:
        with _DIRECT_OPENER.open(request, timeout=timeout) as resp:
            return resp.status == 200
    except OSError:
        return False


def wait_for_qdrant(
    port: int,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
    host: str = cs.LOOPBACK_HOST,
) -> bool:
    # Plain HTTP is deliberate: the stack manager only launches containers on
    # this machine, so the probe targets an address of this machine.
    url = _qdrant_url(host, port, cs.QDRANT_READY_PATH)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_reachable(url):
            return True
        time.sleep(interval)
    return False
