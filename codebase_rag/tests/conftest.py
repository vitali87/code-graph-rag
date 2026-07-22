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

from codebase_rag import graph_audit
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import GraphNodeRecord, GraphRelRecord

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


@pytest.fixture(autouse=True)
def _disable_stack_autostart() -> Generator[None, None, None]:
    from unittest.mock import patch

    with patch("codebase_rag.cli._maybe_start_stack"):
        yield


@pytest.fixture(autouse=True)
def _pin_csharp_frontend_treesitter(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shipped default is AUTO (hybrid wherever dotnet exists), which would
    # run a real MSBuild workspace load in any unit test whose fixture carries
    # a .csproj. Tests pin pure tree-sitter and opt into Roslyn explicitly.
    from codebase_rag import constants as cs
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CSHARP_FRONTEND", cs.CSharpFrontend.TREESITTER)


@pytest.fixture(autouse=True)
def _isolate_vector_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Tests must never touch the developer's real local vector store: a
    # clean-path test would purge it for real, and parallel xdist workers
    # would collide on its file lock.
    from codebase_rag.config import settings

    monkeypatch.setattr(
        settings, "QDRANT_DB_PATH", str(tmp_path_factory.mktemp("qdrant-iso"))
    )


@pytest.fixture(autouse=True)
def _isolate_cgr_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Generator[Path, None, None]:
    from codebase_rag.config import settings

    home = tmp_path_factory.mktemp("cgr-home-iso")
    monkeypatch.setattr(settings, "CGR_HOME", home)
    yield home


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
        self.fetch_all = MagicMock()
        self.execute_write = MagicMock()
        self.ensure_node_batch = MagicMock()
        self.ensure_relationship_batch = MagicMock()
        self.flush_all = MagicMock()
        self._fallback = MagicMock()

    def reset_mock(self) -> None:
        for name in (*self._TRACKED, "_fallback"):
            getattr(self, name).reset_mock()

    @property
    def method_calls(self) -> list:
        result = []
        for name in self._TRACKED:
            mock_attr = self.__dict__[name]
            for c in mock_attr.call_args_list:
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
    _audit_recorded_graph(mock_ingestor)
    return updater


def _audit_recorded_graph(mock_ingestor: MagicMock) -> None:
    """Structural integrity audit of the recorded batches (issue #646).

    Every test that indexes a fixture also asserts the resulting graph is
    schema-conformant, orphan-free, and free of dangling relationships (issue
    #652: an edge with a phantom endpoint is silently dropped by the
    database). CGR_AUDIT_SWEEP=<file> switches to collect mode, appending
    violations as JSON lines instead of failing.
    """
    nodes = [
        GraphNodeRecord(str(c.args[0]), c.args[1])
        for c in mock_ingestor.ensure_node_batch.call_args_list
    ]
    rels = [
        GraphRelRecord(c.args[0], str(c.args[1]), c.args[2])
        for c in mock_ingestor.ensure_relationship_batch.call_args_list
    ]
    violations = graph_audit.collect_violations(nodes, rels)
    if sweep_path := os.environ.get("CGR_AUDIT_SWEEP"):
        import json

        test_id = os.environ.get("PYTEST_CURRENT_TEST", "")
        with open(sweep_path, "a") as f:
            for v in violations:
                f.write(json.dumps([str(v.check), v.detail, test_id]) + "\n")
        return
    assert not violations, "\n".join(v.detail for v in violations)


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
    mock.project_name = temp_repo.resolve().name

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
        import codebase_rag.vector_store as vs

        vs.close_vector_store_client()
    except Exception:
        pass
