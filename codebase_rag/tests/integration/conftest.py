from __future__ import annotations

import socket
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TypedDict

import pytest

from codebase_rag.constants import GraphBackend
from codebase_rag.services.graph import GraphIngestor
from codebase_rag.services.graph.memgraph import MemgraphIngestor

_INTEGRATION_DIR = Path(__file__).parent

# Backends the integration suite runs against.
BACKENDS: tuple[GraphBackend, ...] = (GraphBackend.MEMGRAPH, GraphBackend.ARCADEDB)

MEMGRAPH_IMAGE = "memgraph/memgraph:3.3.0"
MEMGRAPH_READY_LOG = "You are running Memgraph"

ARCADEDB_IMAGE = "arcadedata/arcadedb:26.8.1"
ARCADEDB_TEST_DB = "cgrtest"
ARCADEDB_ROOT_PASSWORD = "cgrtestpassword1!"  # noqa: S105 - throwaway container
ARCADEDB_READY_LOG = "ArcadeDB Server started"
ARCADEDB_BOLT_PLUGIN = "Bolt:com.arcadedb.bolt.BoltProtocolPlugin"


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


def _backend_from_callspec_params(params: dict[str, object]) -> str:
    # Pure helper (no pytest.Item involved) so the classification logic is
    # directly unit-testable. `graph_container` is the only fixture that
    # carries the backend as its indirect-parametrize value; other
    # parametrize axes on the same item must not be mistaken for it, which
    # ruled out the earlier substring-matching approach against the test id.
    backend = params.get("graph_container")
    if backend is None:
        return str(BACKENDS[0])
    return str(backend)


def _backend_of(item: pytest.Item) -> str:
    callspec = getattr(item, "callspec", None)
    if callspec is None:
        return str(BACKENDS[0])
    return _backend_from_callspec_params(callspec.params)


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
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import LogMessageWaitStrategy

    container = DockerContainer(ARCADEDB_IMAGE)
    container.with_exposed_ports(7687, 2480)
    # ARCADEDB_ROOT_PASSWORD as a plain env var is a no-op on this image --
    # decompiling arcadedb-server-26.8.1.jar shows the only thing the server
    # reads is the -Darcadedb.server.rootPassword system property
    # (com.arcadedb.server.security.ServerSecurity). Without it the server
    # falls back to an interactive askForRootPassword prompt, which hangs
    # forever with no TTY attached and the ready log line never appears.
    # The Bolt listener is also a plugin and is off unless explicitly
    # enabled.
    #
    # defaultDatabases' per-database credential entry is `user:password:role`
    # (ArcadeDBServer.parseCredentials); the role is optional but without it
    # ArcadeDBServer.addDatabase grants root access to `cgrtest` with no
    # role, and every schema DDL statement then 403s with "User 'root' is
    # not allowed to update schema" even though root is the server's global
    # superuser. Appending `:admin` grants the role schema DDL needs.
    container.with_env(
        "JAVA_OPTS",
        f"-Darcadedb.server.rootPassword={ARCADEDB_ROOT_PASSWORD} "
        f"-Darcadedb.server.plugins={ARCADEDB_BOLT_PLUGIN} "
        f"-Darcadedb.server.defaultDatabases="
        f"{ARCADEDB_TEST_DB}[root:{ARCADEDB_ROOT_PASSWORD}:admin]",
    )
    container.waiting_for(LogMessageWaitStrategy(ARCADEDB_READY_LOG))
    container.start()

    host = container.get_container_host_ip()
    bolt_port = int(container.get_exposed_port(7687))
    http_port = int(container.get_exposed_port(2480))
    _wait_for_port(host, bolt_port)
    _wait_for_port(host, http_port)
    return container, GraphContainer(
        backend=GraphBackend.ARCADEDB,
        host=host,
        bolt_port=bolt_port,
        http_port=http_port,
        username="root",
        password=ARCADEDB_ROOT_PASSWORD,
    )


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
    # Deferred like _start_arcadedb defers `testcontainers`: arcadedb.py
    # imports neo4j unconditionally, and neo4j ships only in the optional
    # `arcadedb` extra. A module-level import here would break collection of
    # the ENTIRE integration directory -- including every Memgraph-only test
    # -- for any contributor or CI job that didn't sync that extra.
    from codebase_rag.services.graph.arcadedb import ArcadeDBIngestor

    assert info["http_port"] is not None
    assert info["username"] is not None
    assert info["password"] is not None
    return ArcadeDBIngestor(
        host=info["host"],
        bolt_port=info["bolt_port"],
        http_port=info["http_port"],
        database=ARCADEDB_TEST_DB,
        username=info["username"],
        password=info["password"],
    )


def _connect_with_wipe(
    build: Callable[[], GraphIngestor],
) -> Generator[GraphIngestor, None, None]:
    ingestor: GraphIngestor | None = None
    for attempt in range(10):
        candidate = build()
        entered = False
        try:
            candidate.__enter__()
            entered = True
            # Idempotent and cheap on both backends; on ArcadeDB it is load
            # bearing -- MERGE without the unique index is a full type scan,
            # so skipping this would let the conformance suite's idempotency
            # assertions pass by luck on tiny data while masking behaviour
            # that degrades to quadratic on a real repo. This helper backs
            # both `graph_ingestor` and `memgraph_only_ingestor`, so the 30
            # legacy modules that use the latter now also get a constraints
            # setup they never ran under the old `memgraph_ingestor` alias.
            candidate.ensure_constraints()
            candidate.execute_write("MATCH (n) DETACH DELETE n")
            ingestor = candidate
            break
        except Exception as e:
            # __enter__ succeeded but the wipe failed: close the connection
            # and thread pool this attempt opened before retrying, or the
            # next attempt leaks them.
            if entered:
                candidate.__exit__(type(e), e, e.__traceback__)
            if attempt == 9:
                pytest.fail(f"Failed to connect after 10 attempts: {e}")
            time.sleep(0.5)

    assert ingestor is not None
    yield ingestor

    ingestor.execute_write("MATCH (n) DETACH DELETE n")
    ingestor.__exit__(None, None, None)


@pytest.fixture(scope="function")
def graph_ingestor(
    graph_container: GraphContainer,
) -> Generator[GraphIngestor, None, None]:
    yield from _connect_with_wipe(lambda: _build_ingestor(graph_container))


@pytest.fixture(scope="function")
def memgraph_only_ingestor(
    memgraph_container: dict[str, str | int],
) -> Generator[GraphIngestor, None, None]:
    """For tests that white-box a Memgraph-only implementation detail.

    The 30 legacy modules that used to depend on the deprecated
    `memgraph_ingestor` alias have all migrated to the backend-parametrized
    `graph_ingestor`. This fixture remains for the one case that genuinely
    cannot: `TestLegacyPathKeyMigration` in
    `test_cross_project_folder_identity.py` exercises
    `MemgraphIngestor._migrate_legacy_path_keys`, an engine-specific
    migration expressed in Memgraph's proprietary constraint DDL
    (`CREATE/DROP/SHOW CONSTRAINT ON ...`) that ArcadeDB has no equivalent
    for and was never subject to. Deliberately typed as the concrete
    `MemgraphIngestor`, not `GraphIngestor`, so callers can reach its
    private `_execute_query`.
    """
    host = str(memgraph_container["host"])
    port = int(memgraph_container["port"])
    yield from _connect_with_wipe(lambda: MemgraphIngestor(host=host, port=port))


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
