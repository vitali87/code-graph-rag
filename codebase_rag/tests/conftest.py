from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

if TYPE_CHECKING:
    import mgclient  # ty: ignore[unresolved-import]

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


class NodeProtocol(Protocol):
    @property
    def type(self) -> str: ...
    @property
    def children(self) -> list[Self]: ...
    @property
    def parent(self) -> Self | None: ...
    @property
    def text(self) -> bytes: ...
    def child_by_field_name(self, name: str) -> Self | None: ...


@dataclass
class MockNode:
    node_type: str
    node_children: list[MockNode] = field(default_factory=list)
    node_parent: MockNode | None = None
    node_fields: dict[str, MockNode | None] = field(default_factory=dict)
    node_text: bytes = b""

    @property
    def type(self) -> str:
        return self.node_type

    @property
    def children(self) -> list[MockNode]:
        return self.node_children

    @property
    def parent(self) -> MockNode | None:
        return self.node_parent

    @parent.setter
    def parent(self, value: MockNode | None) -> None:
        self.node_parent = value

    @property
    def text(self) -> bytes:
        return self.node_text

    def child_by_field_name(self, name: str) -> MockNode | None:
        return self.node_fields.get(name)


def create_mock_node(
    node_type: str,
    text: str = "",
    fields: dict[str, MockNode | None] | None = None,
    children: list[MockNode] | None = None,
    parent: MockNode | None = None,
) -> MockNode:
    node = MockNode(
        node_type=node_type,
        node_children=children or [],
        node_parent=parent,
        node_fields=fields or {},
        node_text=text.encode(),
    )
    for child in node.node_children:
        child.node_parent = node
    return node


logger.remove()


@pytest.fixture
def temp_repo() -> Generator[Path, None, None]:
    """Creates a temporary repository path for a test and cleans up afterward."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


@pytest.fixture
def mock_ingestor() -> MagicMock:
    """Provides a mocked MemgraphIngestor instance."""
    return MagicMock(spec=MemgraphIngestor)


def run_updater(
    repo_path: Path, mock_ingestor: MagicMock, skip_if_missing: str | None = None
) -> None:
    create_and_run_updater(repo_path, mock_ingestor, skip_if_missing)


def create_and_run_updater(
    repo_path: Path, mock_ingestor: MagicMock, skip_if_missing: str | None = None
) -> GraphUpdater:
    parsers, queries = load_parsers()
    if skip_if_missing and skip_if_missing not in parsers:
        pytest.skip(f"{skip_if_missing} parser not available")
    updater = GraphUpdater(
        ingestor=mock_ingestor,
        repo_path=repo_path,
        parsers=parsers,
        queries=queries,
    )
    updater.run()
    return updater


def get_relationships(mock_ingestor: MagicMock, rel_type: str) -> list:
    """Extract relationships of a specific type from mock_ingestor calls."""
    return [
        c
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
        if c.args[1] == rel_type
    ]


def get_nodes(mock_ingestor: MagicMock, node_type: str) -> list:
    """Extract nodes of a specific type from mock_ingestor calls."""
    return [
        call
        for call in mock_ingestor.ensure_node_batch.call_args_list
        if call[0][0] == node_type
    ]


def get_qualified_names(calls: list) -> set[str]:
    """Extract qualified names from a list of node calls."""
    return {call[0][1]["qualified_name"] for call in calls}


def get_node_names(mock_ingestor: MagicMock, node_type: str) -> set[str]:
    """Get qualified names of all nodes of a specific type."""
    return get_qualified_names(get_nodes(mock_ingestor, node_type))


@pytest.fixture
def mock_updater(temp_repo: Path, mock_ingestor: MagicMock) -> MagicMock:
    """Provides a mocked GraphUpdater instance with necessary dependencies."""
    parsers, queries = load_parsers()
    mock = MagicMock(spec=GraphUpdater)
    mock.repo_path = temp_repo
    mock.ingestor = mock_ingestor
    mock.parsers = parsers
    mock.queries = queries

    mock.factory = MagicMock()
    mock.factory.definition_processor = MagicMock()
    mock.factory.structure_processor = MagicMock()
    mock.factory.structure_processor.structural_elements = {}

    mock_root_node = MagicMock()
    mock.factory.definition_processor.process_file.return_value = (
        mock_root_node,
        "python",
    )

    mock.ast_cache = {}

    return mock


@pytest.fixture(scope="session")
def memgraph_container() -> Generator[dict[str, str | int], None, None]:
    pytest.importorskip("testcontainers")
    import socket
    import time

    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer("memgraph/memgraph:latest")
    container.with_exposed_ports(7687)

    container.start()
    wait_for_logs(container, "You are running Memgraph", timeout=60)

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
    import time

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
    import time

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
