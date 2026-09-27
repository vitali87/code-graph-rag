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
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
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


def qdrant_accepts_anonymous(port: int, timeout: float = 1.5) -> bool:
    return _qdrant_data_reachable(port, {}, timeout)


def qdrant_accepts_key(port: int, api_key: str, timeout: float = 1.5) -> bool:
    return _qdrant_data_reachable(port, {cs.QDRANT_API_KEY_HEADER: api_key}, timeout)


def _qdrant_data_reachable(port: int, headers: dict[str, str], timeout: float) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{cs.QDRANT_DATA_PROBE_PATH}", headers=headers
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
) -> bool:
    # Plain HTTP is deliberate: the stack manager only launches containers on
    # this machine, so the probe is pinned to loopback.
    url = f"http://127.0.0.1:{port}/readyz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_reachable(url):
            return True
        time.sleep(interval)
    return False
