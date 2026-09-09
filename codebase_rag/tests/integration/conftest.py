from __future__ import annotations

import socket
import time
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.tests.container_reaper import (
    cgr_container_labels,
    reap_orphaned_containers,
)

if TYPE_CHECKING:
    import mgclient

_INTEGRATION_DIR = Path(__file__).parent


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # Every integration test wipes the whole Memgraph database, and under xdist
    # a session-scoped container fixture is per-worker, so -n auto races one
    # container startup per worker. Pinning the directory to one xdist_group
    # serialises them onto one worker with one container (--dist=loadgroup).
    for item in items:
        # Third-party plugins can collect virtual items with no path.
        if item.path and _INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.xdist_group("memgraph-integration"))


@pytest.fixture(scope="session")
def memgraph_container() -> Generator[dict[str, str | int], None, None]:
    pytest.importorskip("testcontainers")
    import time

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.docker_client import DockerClient
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    # A previous run that was killed (OOM, CI timeout, kill -9) never reached
    # the `container.stop()` below and left its container running; each one
    # is a permanent charge against the memory the next run needs (issue
    # #1628). Nothing in-process survives being killed, so the next session
    # cleans up for the last one, by the labels this fixture puts on its own
    # container: the marker, and the host and pid that own it, so a session
    # sharing the daemon with a LIVE suite leaves that suite's database alone.
    reap_orphaned_containers(DockerClient().client)

    # Same engine line the packaged stack pins (issue #1257): integration
    # tests must exercise the syntax the shipped Memgraph actually accepts.
    container = DockerContainer("memgraph/memgraph:3.3.0")
    container.with_exposed_ports(7687)
    container.with_kwargs(labels=cgr_container_labels())
    container.waiting_for(LogMessageWaitStrategy("You are running Memgraph"))

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


@pytest.fixture(scope="session")
def neo4j_container() -> Generator[dict[str, str | int], None, None]:
    """A real Neo4j 5 server for the backend-parity tests (issue #1590).

    Skipped rather than failed when the driver or Docker is unavailable:
    `neo4j` is an optional extra, so a default install must still be able
    to run the suite.
    """
    pytest.importorskip("testcontainers")
    pytest.importorskip("neo4j")

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.docker_client import DockerClient
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    reap_orphaned_containers(DockerClient().client)

    container = DockerContainer("neo4j:5.26")
    container.with_exposed_ports(7687)
    container.with_kwargs(labels=cgr_container_labels())
    # Auth off keeps the fixture in step with the unauthenticated Memgraph
    # container above, so the parity tests differ only by engine.
    container.with_env("NEO4J_AUTH", "none")
    container.waiting_for(LogMessageWaitStrategy("Started."))
    container.start()

    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(7687))

    # "Started." precedes Bolt actually accepting queries, so probe with a
    # real statement rather than trusting the log line or a bare socket.
    from neo4j import GraphDatabase

    max_retries = 30
    for attempt in range(max_retries):
        try:
            driver = GraphDatabase.driver(f"bolt://{host}:{port}", auth=None)
            with driver.session() as session:
                session.run("RETURN 1").consume()
            driver.close()
            break
        except Exception as exc:
            if attempt == max_retries - 1:
                container.stop()
                pytest.fail(f"Neo4j not ready after {max_retries} attempts: {exc}")
            time.sleep(1.0)

    yield {"host": host, "port": port}

    container.stop()


@pytest.fixture
def neo4j_ingestor(
    neo4j_container: dict[str, str | int],
) -> Generator[MemgraphIngestor, None, None]:
    """The ingestor pointed at Neo4j, wiped before and after each test."""
    from codebase_rag.config import settings
    from codebase_rag.graph_dialects import DIALECT_NEO4J, get_dialect

    host = str(neo4j_container["host"])
    port = int(neo4j_container["port"])

    previous_uri = settings.NEO4J_URI
    settings.NEO4J_URI = f"bolt://{host}:{port}"
    ingestor = MemgraphIngestor(
        host=host, port=port, dialect=get_dialect(DIALECT_NEO4J)
    )
    ingestor.__enter__()
    try:
        ingestor._execute_query("MATCH (n) DETACH DELETE n")
        yield ingestor
    finally:
        try:
            ingestor._execute_query("MATCH (n) DETACH DELETE n")
        finally:
            ingestor.__exit__(None, None, None)
            settings.NEO4J_URI = previous_uri


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


@pytest.fixture(scope="function")
def memgraph_ingestor(
    memgraph_container: dict[str, str | int],
) -> Generator[MemgraphIngestor, None, None]:
    host = str(memgraph_container["host"])
    port = int(memgraph_container["port"])

    max_retries = 10

    for attempt in range(max_retries):
        try:
            ingestor = MemgraphIngestor(host=host, port=port)
            ingestor.__enter__()
            ingestor._execute_query("MATCH (n) DETACH DELETE n")
            break
        except Exception as e:
            if attempt == max_retries - 1:
                pytest.fail(
                    f"Failed to connect to Memgraph after {max_retries} attempts: {e}"
                )
            time.sleep(0.5)

    yield ingestor

    ingestor._execute_query("MATCH (n) DETACH DELETE n")
    ingestor.__exit__(None, None, None)
