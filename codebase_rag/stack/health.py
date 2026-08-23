from __future__ import annotations

import time
import urllib.error
import urllib.request

import mgclient  # ty: ignore[unresolved-import]

from ..config import settings
from ..constants import GraphBackend
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


def _arcade_bolt_reachable(host: str, port: int) -> bool:
    from neo4j import GraphDatabase

    from codebase_rag.config import settings

    try:
        driver = GraphDatabase.driver(
            f"bolt://{host}:{port}",
            auth=(settings.ARCADEDB_USERNAME or "", settings.ARCADEDB_PASSWORD or ""),
        )
        try:
            driver.verify_connectivity()
        finally:
            driver.close()
        return True
    except Exception:
        return False


def _http_reachable(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _arcade_ready_url(host: str, http_port: int | None) -> str:
    # http_port is `int | None` because this module also serves Memgraph
    # (which has no HTTP probe), but every ArcadeDB caller must supply it --
    # a silent `None` here would build ".../None/api/v1/ready" and just
    # report ArcadeDB unreachable instead of surfacing the caller bug.
    if http_port is None:
        raise ValueError(cs.ERR_ARCADE_HTTP_PORT_REQUIRED)
    # Matches ArcadeHttpClient's scheme (issue: CodeRabbit review, the
    # probe used to hardcode http:// regardless of ARCADEDB_HTTP_SCHEME).
    return f"{settings.ARCADEDB_HTTP_SCHEME}://{host}:{http_port}/api/v1/ready"


def wait_for_graph(
    backend: GraphBackend,
    host: str,
    bolt_port: int,
    http_port: int | None,
    timeout: float = cs.DEFAULT_HEALTH_TIMEOUT_S,
    interval: float = cs.DEFAULT_HEALTH_INTERVAL_S,
) -> bool:
    # Resolved once up front, outside the poll loop: it is a fixed fact
    # about the call (not something that can start being true), and a
    # caller bug should fail immediately rather than after `timeout`
    # seconds of silently polling a malformed URL.
    ready_url = (
        _arcade_ready_url(host, http_port) if backend == GraphBackend.ARCADEDB else None
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if backend == GraphBackend.ARCADEDB:
            # Both transports must answer: schema DDL goes over HTTP, so a
            # live Bolt listener alone would pass startup then fail at
            # ensure_constraints().
            assert ready_url is not None
            if _arcade_bolt_reachable(host, bolt_port) and _http_reachable(ready_url):
                return True
        elif _bolt_reachable(host, bolt_port):
            return True
        time.sleep(interval)
    return False


def graph_reachability_detail(
    backend: GraphBackend,
    host: str,
    bolt_port: int,
    http_port: int | None,
) -> str | None:
    """One-shot diagnosis for a graph backend `status()` found unreachable.

    A single `reachable=False` boolean cannot tell an operator whether
    ArcadeDB's Bolt listener or its HTTP endpoint is the one that's down --
    "Bolt up, HTTP down" is precisely the split-brain failure the two-port
    probe in `wait_for_graph` exists to catch (a live Bolt connection would
    otherwise pass startup and only fail later, inside
    `ensure_constraints()`). Returns None when there is nothing to
    disambiguate: the backend is reachable, or it is Memgraph, which only
    has the one port `wait_for_graph` already checked.
    """
    if backend != GraphBackend.ARCADEDB:
        return None
    bolt_ok = _arcade_bolt_reachable(host, bolt_port)
    http_ok = _http_reachable(_arcade_ready_url(host, http_port))
    if bolt_ok and http_ok:
        return None
    if not bolt_ok and not http_ok:
        return "bolt unreachable, http unreachable"
    if not bolt_ok:
        return "bolt unreachable, http ok"
    return "bolt ok, http unreachable (schema changes will fail)"


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
