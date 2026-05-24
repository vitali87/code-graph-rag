from __future__ import annotations

import socket
import time
import urllib.error
import urllib.request

from . import constants as cs


def _tcp_reachable(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
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
        if _tcp_reachable(host, port):
            return True
        time.sleep(interval)
    return False


def wait_for_qdrant(
    host: str,
    port: int,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
) -> bool:
    url = f"http://{host}:{port}/readyz"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_reachable(url):
            return True
        time.sleep(interval)
    return False
