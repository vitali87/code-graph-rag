# Real-Memgraph check of what the MCP guide says after issue #2678: two
# servers, one per repository, share one graph, and `index_repository` on
# one of them rebuilds its own project and leaves the other's nodes alone.
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.utils.path_utils import derive_project_name

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def no_vector_store() -> Iterator[None]:
    # No Qdrant in this test; the graph is what the guide describes.
    with patch("codebase_rag.mcp.tools.delete_project_embeddings"):
        yield


def _repo(root: Path, name: str, source: str) -> Path:
    repo = root / name
    repo.mkdir()
    (repo / "app.py").write_text(source, encoding="utf-8")
    return repo


def _server(repo: Path, ingestor: MemgraphIngestor) -> MCPToolsRegistry:
    return MCPToolsRegistry(
        project_root=str(repo), ingestor=ingestor, cypher_gen=MagicMock()
    )


def _functions(ingestor: MemgraphIngestor, project: str) -> list[str]:
    rows = ingestor.fetch_all(
        "MATCH (f:Function) WHERE f.qualified_name STARTS WITH ($p + '.') "
        "RETURN f.qualified_name AS qn ORDER BY qn",
        {"p": project},
    )
    return [str(row["qn"]) for row in rows]


async def test_indexing_one_repository_leaves_the_other_project(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    backend = _repo(tmp_path, "backend", "def serve():\n    return 1\n")
    frontend = _repo(tmp_path, "frontend", "def render():\n    return 2\n")
    backend_project = derive_project_name(backend)
    frontend_project = derive_project_name(frontend)

    await _server(backend, memgraph_ingestor).index_repository()
    await _server(frontend, memgraph_ingestor).index_repository()
    (frontend / "app.py").write_text("def paint():\n    return 3\n", encoding="utf-8")
    await _server(frontend, memgraph_ingestor).index_repository()

    assert set(memgraph_ingestor.list_projects()) >= {
        backend_project,
        frontend_project,
    }
    assert _functions(memgraph_ingestor, backend_project) == [
        f"{backend_project}.app.serve"
    ]
    assert _functions(memgraph_ingestor, frontend_project) == [
        f"{frontend_project}.app.paint"
    ]
