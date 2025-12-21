from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor

if TYPE_CHECKING:
    import mgclient  # ty: ignore[unresolved-import]

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
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
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer("memgraph/memgraph:latest")
    container.with_exposed_ports(7687)

    container.start()
    wait_for_logs(container, "You are running Memgraph", timeout=60)

    host = container.get_container_host_ip()
    port = int(container.get_exposed_port(7687))

    yield {"host": host, "port": port}

    container.stop()


@pytest.fixture(scope="function")
def memgraph_connection(
    memgraph_container: dict[str, str | int],
) -> Generator[mgclient.Connection, None, None]:
    import mgclient  # ty: ignore[unresolved-import]

    conn = mgclient.connect(
        host=str(memgraph_container["host"]),
        port=int(memgraph_container["port"]),
    )
    conn.autocommit = True

    cursor = conn.cursor()
    cursor.execute("MATCH (n) DETACH DELETE n")
    cursor.close()

    yield conn

    cursor = conn.cursor()
    cursor.execute("MATCH (n) DETACH DELETE n")
    cursor.close()
    conn.close()


@pytest.fixture(scope="function")
def memgraph_ingestor(
    memgraph_container: dict[str, str | int],
) -> Generator[MemgraphIngestor, None, None]:
    ingestor = MemgraphIngestor(
        host=str(memgraph_container["host"]),
        port=int(memgraph_container["port"]),
    )
    ingestor.__enter__()

    ingestor._execute_query("MATCH (n) DETACH DELETE n")

    yield ingestor

    ingestor._execute_query("MATCH (n) DETACH DELETE n")
    ingestor.__exit__(None, None, None)
