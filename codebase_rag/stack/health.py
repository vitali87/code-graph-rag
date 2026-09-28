from __future__ import annotations

import time
import urllib.error
import urllib.request

import mgclient  # ty: ignore[unresolved-import]

from . import constants as cs

# The Qdrant probes can carry the API key, and urllib's default opener would
# route even a loopback request through an HTTP_PROXY that no_proxy does not
# exempt, handing the key to the proxy.
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


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
            cursor.execute("RETURN 1")
            cursor.fetchall()
        finally:
            conn.close()
        return True
    except (mgclient.Error, OSError):
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
    return _bolt_reachable(host, port)


def memgraph_rejects_credentials(
    host: str, port: int, credentials: tuple[str, str]
) -> bool:
    username, password = credentials
    try:
        conn = mgclient.connect(
            host=host, port=port, username=username, password=password
        )
    except mgclient.Error as e:
        return cs.MEMGRAPH_AUTH_FAILURE in str(e)
    except OSError:
        return False
    conn.close()
    return False


def qdrant_accepts_anonymous(
    port: int, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> bool:
    return _qdrant_data_reachable(host, port, {}, timeout)


def qdrant_accepts_key(
    port: int, api_key: str, timeout: float = 1.5, host: str = cs.LOOPBACK_HOST
) -> bool:
    return _qdrant_data_reachable(
        host, port, {cs.QDRANT_API_KEY_HEADER: api_key}, timeout
    )


def _qdrant_url(host: str, port: int, path: str) -> str:
    # An IPv6 address needs brackets in a URL.
    netloc = f"[{host}]" if ":" in host else host
    return f"http://{netloc}:{port}{path}"


def _qdrant_data_reachable(
    host: str, port: int, headers: dict[str, str], timeout: float
) -> bool:
    request = urllib.request.Request(
        _qdrant_url(host, port, cs.QDRANT_DATA_PROBE_PATH), headers=headers
    )
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
