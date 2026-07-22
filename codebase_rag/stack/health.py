from __future__ import annotations

import time
import urllib.error
import urllib.request

import mgclient  # ty: ignore[unresolved-import]

from . import constants as cs


def _bolt_reachable(host: str, port: int) -> bool:
    try:
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
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_for_memgraph(
    host: str,
    port: int,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _bolt_reachable(host, port):
            return True
        time.sleep(interval)
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
