from __future__ import annotations

import time
import urllib.error
import urllib.request
from http.client import HTTPMessage
from typing import IO

import mgclient

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
