from __future__ import annotations

import socket
import time
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.memgraph import MemgraphIngestor

if TYPE_CHECKING:
    import mgclient

_INTEGRATION_DIR = Path(__file__).parent

# Backends the integration suite runs against. Task 14 appends ARCADEDB.
BACKENDS: tuple[GraphBackend, ...] = (GraphBackend.MEMGRAPH,)

MEMGRAPH_IMAGE = "memgraph/memgraph:3.3.0"
MEMGRAPH_READY_LOG = "You are running Memgraph"


class GraphContainer(TypedDict):
    backend: GraphBackend
    host: str
    bolt_port: int
    http_port: int | None
    username: str | None
    password: str | None


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Every integration test wipes the whole database, so tests sharing a
    # container must not run concurrently. Grouping PER BACKEND (rather than
    # one group for the whole directory) keeps each backend serialised while
    # letting the two backends run on different xdist workers, so wall clock
    # stays near 1x instead of 2x.
    for item in items:
        if not (item.path and _INTEGRATION_DIR in item.path.parents):
            continue
        backend = _backend_of(item)
        item.add_marker(pytest.mark.xdist_group(f"graph-integration-{backend}"))


def _backend_of(item: pytest.Item) -> str:
    # Parametrised items carry the backend in their id as [memgraph] etc.
    for backend in BACKENDS:
        if f"[{backend}" in item.name or f"-{backend}]" in item.name:
            return str(backend)
    return str(BACKENDS[0])


def _wait_for_port(host: str, port: int, attempts: int = 30) -> None:
    for attempt in range(attempts):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect((host, port))
            sock.close()
            return
        except (TimeoutError, ConnectionRefusedError, OSError):
            if attempt == attempts - 1:
                pytest.fail(f"port {port} on {host} not ready after {attempts} tries")
            time.sleep(0.5)


def _start_memgraph() -> tuple[object, GraphContainer]:
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    # Same engine line the packaged stack pins (issue #1257): integration
    # tests must exercise the syntax the shipped Memgraph actually accepts.
    container = DockerContainer(MEMGRAPH_IMAGE)
    container.with_exposed_ports(7687)
    container.waiting_for(LogMessageWaitStrategy(MEMGRAPH_READY_LOG))
    container.start()

    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(7687))
    _wait_for_port(host, port)
    return container, GraphContainer(
        backend=GraphBackend.MEMGRAPH,
        host=host,
        bolt_port=port,
        http_port=None,
        username=None,
        password=None,
    )


def _start_arcadedb() -> tuple[object, GraphContainer]:
    raise NotImplementedError


@pytest.fixture(scope="session", params=BACKENDS, ids=[str(b) for b in BACKENDS])
def graph_container(
    request: pytest.FixtureRequest,
) -> Generator[GraphContainer, None, None]:
    pytest.importorskip("testcontainers")
    backend: GraphBackend = request.param
    if backend == GraphBackend.MEMGRAPH:
        container, info = _start_memgraph()
    else:
        container, info = _start_arcadedb()
    try:
        yield info
    finally:
        container.stop()


def _build_ingestor(info: GraphContainer) -> GraphIngestor:
    if info["backend"] == GraphBackend.MEMGRAPH:
        return MemgraphIngestor(host=info["host"], port=info["bolt_port"])
    # Unreachable while BACKENDS holds only MEMGRAPH. Task 14 adds
    # ArcadeDBIngestor and ARCADEDB_TEST_DB and replaces this placeholder.
    raise NotImplementedError


@pytest.fixture(scope="function")
def graph_ingestor(
    graph_container: GraphContainer,
) -> Generator[GraphIngestor, None, None]:
    ingestor: GraphIngestor | None = None
    for attempt in range(10):
        try:
            ingestor = _build_ingestor(graph_container)
            ingestor.__enter__()
            ingestor.execute_write("MATCH (n) DETACH DELETE n")
            break
        except Exception as e:
            if attempt == 9:
                pytest.fail(f"Failed to connect after 10 attempts: {e}")
            time.sleep(0.5)

    assert ingestor is not None
    yield ingestor

    ingestor.execute_write("MATCH (n) DETACH DELETE n")
    ingestor.__exit__(None, None, None)


@pytest.fixture(scope="function")
def memgraph_ingestor(graph_ingestor: GraphIngestor) -> GraphIngestor:
    """Deprecated alias. Retained so the 30 existing test modules keep
    working while they migrate; delete once none reference it."""
    return graph_ingestor


@pytest.fixture(scope="session")
def memgraph_container() -> Generator[dict[str, str | int], None, None]:
    pytest.importorskip("testcontainers")

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    # Same engine line the packaged stack pins (issue #1257): integration
    # tests must exercise the syntax the shipped Memgraph actually accepts.
    container = DockerContainer(MEMGRAPH_IMAGE)
    container.with_exposed_ports(7687)
    container.waiting_for(LogMessageWaitStrategy(MEMGRAPH_READY_LOG))

    container.start()

    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(7687))

    max_retries = 30
    for attempt in range(max_retries):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            sock.connect((host, port))
            sock.close()
            break
        except (TimeoutError, ConnectionRefusedError, OSError):
            if attempt == max_retries - 1:
                container.stop()
                pytest.fail(
                    f"Memgraph port {port} not ready after {max_retries} attempts"
                )
            time.sleep(0.5)

    yield {"host": host, "port": port}

    container.stop()


@pytest.fixture(scope="function")
def memgraph_connection(
    memgraph_container: dict[str, str | int],
) -> Generator[mgclient.Connection, None, None]:
    import mgclient  # ty: ignore[unresolved-import]

    host = str(memgraph_container["host"])
    port = int(memgraph_container["port"])

    max_retries = 10
    conn: mgclient.Connection | None = None

    for attempt in range(max_retries):
        try:
            conn = mgclient.connect(host=host, port=port)
            conn.autocommit = True
            cursor = conn.cursor()
            cursor.execute("MATCH (n) DETACH DELETE n")
            cursor.close()
            break
        except Exception as e:
            if attempt == max_retries - 1:
                pytest.fail(
                    f"Failed to connect to Memgraph after {max_retries} attempts: {e}"
                )
            time.sleep(0.5)

    if conn is None:
        pytest.fail("Failed to establish Memgraph connection")

    yield conn

    assert conn is not None
    cursor = conn.cursor()
    cursor.execute("MATCH (n) DETACH DELETE n")
    cursor.close()
    conn.close()
