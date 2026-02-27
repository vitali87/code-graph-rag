from __future__ import annotations

import os
import shutil
import sys
import tempfile
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from unittest.mock import MagicMock, call

import pytest
from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryProtocol

if TYPE_CHECKING:
    pass  # ty: ignore[unresolved-import]

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


class _MockIngestor:
    _TRACKED = (
        "fetch_all",
        "execute_write",
        "ensure_node_batch",
        "ensure_relationship_batch",
        "flush_all",
    )

    def __init__(self) -> None:
        object.__setattr__(self, "_mocks", {n: MagicMock() for n in self._TRACKED})
        object.__setattr__(self, "_fallback", MagicMock())

    @property
    def fetch_all(self) -> MagicMock:
        return self._mocks["fetch_all"]

    @property
    def execute_write(self) -> MagicMock:
        return self._mocks["execute_write"]

    @property
    def ensure_node_batch(self) -> MagicMock:
        return self._mocks["ensure_node_batch"]

    @property
    def ensure_relationship_batch(self) -> MagicMock:
        return self._mocks["ensure_relationship_batch"]

    @property
    def flush_all(self) -> MagicMock:
        return self._mocks["flush_all"]

    def reset_mock(self) -> None:
        for m in self._mocks.values():
            m.reset_mock()
        self._fallback.reset_mock()

    @property
    def method_calls(self) -> list:
        result = []
        for name, mock in self._mocks.items():
            for c in mock.call_args_list:
                result.append(getattr(call, name)(*c.args, **c.kwargs))
        result.extend(self._fallback.method_calls)
        return result

    def __getattr__(self, name: str) -> MagicMock:
        return getattr(self._fallback, name)


@pytest.fixture
def mock_ingestor() -> _MockIngestor:
    return _MockIngestor()


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


@pytest.fixture(scope="session", autouse=True)
def cleanup_qdrant_client() -> Generator[None, None, None]:
    yield

    try:
        from codebase_rag.utils.dependencies import has_qdrant_client

        if has_qdrant_client():
            import codebase_rag.vector_store as vs

            if vs._CLIENT is not None:
                try:
                    vs._CLIENT.close()
                except Exception:
                    pass
                vs._CLIENT = None
    except Exception:
        pass
