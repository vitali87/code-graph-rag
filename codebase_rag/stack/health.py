from __future__ import annotations

import functools
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from http.client import HTTPMessage
from typing import IO, Protocol

import mgclient
from loguru import logger

from .. import constants as root_cs
from . import constants as cs

if sys.platform == "win32":  # pragma: no cover - platform
    import _winapi
    import ctypes

    # The standard library's msvcrt module wraps the UCRT, not msvcrt.dll.
    import msvcrt

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
    # in another.
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


def _c_runtime_result(result: int) -> int:
    if result == -1:
        raise OSError(cs.ERR_C_RUNTIME_CALL_FAILED)
    return result


if sys.platform == "win32":  # pragma: no cover - platform

    class _MsvcrtCRuntime:
        # mgclient's perror() and fprintf(stderr) go through msvcrt.dll there,
        # to msvcrt.dll's fd 2, which os.dup2 on the UCRT's fd 2 never reaches.
        def __init__(self, crt: ctypes.CDLL) -> None:
            crt._open_osfhandle.argtypes = (ctypes.c_ssize_t, ctypes.c_int)
            self._crt = crt

        def dup(self, fd: int) -> int:
            return _c_runtime_result(self._crt._dup(fd))

        def dup2(self, fd: int, fd2: int) -> None:
            # msvcrt.dll fully buffers stderr whenever fd 2 is not a console,
            # as the capture file is not. Flushing before fd 2 moves sends
            # each message to the file fd 2 named when it was printed.
            self._crt.fflush(None)
            _c_runtime_result(self._crt._dup2(fd, fd2))

        def close(self, fd: int) -> None:
            _c_runtime_result(self._crt._close(fd))

        def open_file(self, file: IO[bytes]) -> int:
            # A descriptor closes its handle with it, so this one gets its own
            # handle rather than sharing the one Python's descriptor closes.
            process = _winapi.GetCurrentProcess()
            handle = _winapi.DuplicateHandle(
                process,
                msvcrt.get_osfhandle(file.fileno()),
                process,
                0,
                False,
                _winapi.DUPLICATE_SAME_ACCESS,
            )
            fd = self._crt._open_osfhandle(handle, os.O_BINARY)
            if fd == -1:
                _winapi.CloseHandle(handle)
            return _c_runtime_result(fd)


@functools.cache
def _mgclient_own_c_runtime() -> _CRuntime | None:
    # The C runtime besides Python's that mgclient prints through, if any.
    if sys.platform != "win32":
        return None
    return _MsvcrtCRuntime(  # pragma: no cover - platform
        ctypes.CDLL(cs.MGCLIENT_WINDOWS_C_RUNTIME)
    )


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
            with ExitStack() as redirects:
                redirects.enter_context(_stderr_into(_PYTHON_C_RUNTIME, capture))
                if (own_runtime := _mgclient_own_c_runtime()) is not None:
                    redirects.enter_context(_stderr_into(own_runtime, capture))
                yield
        finally:
            # Kept rather than dropped, for whoever is chasing a Memgraph that
            # never answers; the probe's own result is what reports it.
            capture.seek(0)
            output = capture.read().decode(root_cs.ENCODING_UTF8, errors="replace")
            if output := output.strip():
                logger.debug(cs.MSG_MEMGRAPH_PROBE_OUTPUT.format(output=output))


@_mgclient_stderr_to_debug_log()
def _bolt_reachable(
    host: str, port: int, credentials: tuple[str, str] | None = None
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


@_mgclient_stderr_to_debug_log()
def memgraph_anonymous_access(host: str, port: int) -> cs.AnonymousAccess:
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


@_mgclient_stderr_to_debug_log()
def memgraph_rejects_credentials(
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
