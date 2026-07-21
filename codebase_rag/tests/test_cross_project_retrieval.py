"""Cross-project retrieval must work from any working directory (issue #425).

Nodes store ``absolute_path`` at index time; retrieval tools must fall back to
it when the relative ``path`` does not resolve against the current project
root, so snippets from other indexed projects (or after a cwd change) are
still readable. MCP indexing must also derive the same collision-resistant
project name as the CLI, otherwise two repos with the same directory name
delete each other's graphs.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.cypher_queries import (
    CYPHER_FIND_BY_QUALIFIED_NAME,
    CYPHER_GET_FUNCTION_SOURCE_LOCATION,
)
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.tools.code_retrieval import CodeRetriever
from codebase_rag.tools.semantic_search import get_function_source_code
from codebase_rag.utils.path_utils import derive_project_name

_SOURCE = "def get_user(user_id):\n    return user_id\n"


class TestFindSnippetAcrossProjects:
    @pytest.mark.asyncio
    async def test_falls_back_to_absolute_path_when_outside_project_root(
        self, tmp_path: Path
    ) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()
        other_repo = tmp_path / "user-service" / "src"
        other_repo.mkdir(parents=True)
        target = other_repo / "handlers.py"
        target.write_text(_SOURCE, encoding="utf-8")

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(target),
                "start": 1,
                "end": 2,
                "docstring": None,
            }
        ]
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet("user-service.src.handlers.get_user")

        assert result.found is True
        assert result.source_code == _SOURCE

    @pytest.mark.asyncio
    async def test_prefers_project_root_when_relative_path_resolves(
        self, tmp_path: Path
    ) -> None:
        current_repo = tmp_path / "order-service"
        (current_repo / "src").mkdir(parents=True)
        local = current_repo / "src" / "handlers.py"
        local.write_text(_SOURCE, encoding="utf-8")
        stale = tmp_path / "stale" / "handlers.py"
        stale.parent.mkdir()
        stale.write_text("# stale copy\n", encoding="utf-8")

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(stale),
                "start": 1,
                "end": 2,
                "docstring": None,
            }
        ]
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet(
            "order-service.src.handlers.get_user"
        )

        assert result.found is True
        assert result.source_code == _SOURCE

    def test_find_by_qualified_name_returns_absolute_path(self) -> None:
        assert "absolute_path" in CYPHER_FIND_BY_QUALIFIED_NAME


class TestFunctionSourceAcrossProjects:
    def test_uses_absolute_path_when_relative_path_missing(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "handlers.py"
        target.write_text(_SOURCE, encoding="utf-8")

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "qualified_name": "user-service.handlers.get_user",
                "path": "definitely/not/here/handlers.py",
                "absolute_path": str(target),
                "start_line": 1,
                "end_line": 2,
            }
        ]

        source = get_function_source_code(ingestor, node_id=1)

        assert source is not None
        assert "def get_user" in source

    def test_source_location_query_returns_absolute_path(self) -> None:
        assert "absolute_path" in CYPHER_GET_FUNCTION_SOURCE_LOCATION


class TestMcpProjectNaming:
    @pytest.fixture
    def registry(self, tmp_path: Path) -> MCPToolsRegistry:
        repo = tmp_path / "backend"
        repo.mkdir()
        return MCPToolsRegistry(
            project_root=str(repo),
            ingestor=MagicMock(),
            cypher_gen=MagicMock(),
        )

    def test_index_repository_uses_derived_project_name(
        self, registry: MCPToolsRegistry
    ) -> None:
        derived = derive_project_name(Path(registry.project_root))
        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            registry._index_repository_sync()

        ingestor = registry.ingestor
        ingestor.delete_project.assert_called_once_with(derived)
        assert updater_cls.call_args.kwargs["project_name"] == derived

    def test_update_repository_uses_derived_project_name(
        self, registry: MCPToolsRegistry
    ) -> None:
        derived = derive_project_name(Path(registry.project_root))
        with patch("codebase_rag.mcp.tools.GraphUpdater") as updater_cls:
            registry._update_repository_sync()

        assert updater_cls.call_args.kwargs["project_name"] == derived


class TestMcpServerProjectScope:
    def test_cypher_generator_scoped_to_target_repo_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "backend"
        repo.mkdir()
        monkeypatch.setenv("TARGET_REPO_PATH", str(repo))

        from codebase_rag.mcp import server as srv

        with (
            patch.object(srv, "MemgraphIngestor"),
            patch.object(srv, "CypherGenerator") as cypher_cls,
            patch.object(srv, "create_mcp_tools_registry"),
        ):
            srv.create_server()

        assert cypher_cls.call_args.kwargs.get("active_projects") == [
            derive_project_name(repo)
        ]
