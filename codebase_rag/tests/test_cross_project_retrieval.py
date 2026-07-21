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
    CYPHER_LIST_PROJECTS,
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
    async def test_absolute_path_wins_over_colliding_local_file(
        self, tmp_path: Path
    ) -> None:
        # The node's recorded location is authoritative: a same-named file in
        # the active repo must not shadow a cross-project node.
        current_repo = tmp_path / "order-service"
        (current_repo / "src").mkdir(parents=True)
        (current_repo / "src" / "handlers.py").write_text(
            "# unrelated local file\n", encoding="utf-8"
        )
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
    async def test_falls_back_to_project_root_when_absolute_path_stale(
        self, tmp_path: Path
    ) -> None:
        # The repo moved since indexing: the recorded absolute_path is gone
        # but the relative join against the current root still resolves.
        current_repo = tmp_path / "order-service"
        (current_repo / "src").mkdir(parents=True)
        (current_repo / "src" / "handlers.py").write_text(_SOURCE, encoding="utf-8")

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(tmp_path / "gone" / "handlers.py"),
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

    @pytest.mark.asyncio
    async def test_not_found_when_no_file_exists_anywhere(self, tmp_path: Path) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(tmp_path / "gone" / "handlers.py"),
                "start": 1,
                "end": 2,
                "docstring": None,
            }
        ]
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet(
            "order-service.src.handlers.get_user"
        )

        assert result.found is False
        assert result.error_message is not None
        assert "Source file not found" in result.error_message

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

    def test_absolute_path_wins_over_cwd_collision(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file in the process CWD matching the node's relative path must
        # not shadow the recorded absolute location.
        cwd = tmp_path / "elsewhere"
        (cwd / "src").mkdir(parents=True)
        (cwd / "src" / "handlers.py").write_text(
            "# unrelated cwd file\n", encoding="utf-8"
        )
        monkeypatch.chdir(cwd)

        target = tmp_path / "user-service" / "src" / "handlers.py"
        target.parent.mkdir(parents=True)
        target.write_text(_SOURCE, encoding="utf-8")

        ingestor = MagicMock()
        ingestor.fetch_all.return_value = [
            {
                "qualified_name": "user-service.src.handlers.get_user",
                "path": "src/handlers.py",
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


class TestBoundedAbsolutePathReads:
    """A recorded absolute_path is only honored inside its project's indexed
    root; unknown roots (legacy graphs) keep the permissive behavior."""

    def _ingestor(self, target: Path, roots: dict[str, str | None]) -> MagicMock:
        node_rows = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(target),
                "start": 1,
                "end": 2,
                "docstring": None,
            }
        ]
        roots_rows = [{"name": name, "root_path": root} for name, root in roots.items()]
        ingestor = MagicMock()
        ingestor.fetch_all.side_effect = lambda query, params=None: (
            roots_rows if query == CYPHER_LIST_PROJECTS else node_rows
        )
        return ingestor

    @pytest.mark.asyncio
    async def test_rejects_absolute_path_outside_recorded_root(
        self, tmp_path: Path
    ) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()
        outside = tmp_path / "elsewhere" / "handlers.py"
        outside.parent.mkdir(parents=True)
        outside.write_text(_SOURCE, encoding="utf-8")

        ingestor = self._ingestor(
            outside, {"user-service": str(tmp_path / "user-service")}
        )
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet("user-service.src.handlers.get_user")

        assert result.found is False

    @pytest.mark.asyncio
    async def test_allows_absolute_path_inside_recorded_root(
        self, tmp_path: Path
    ) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()
        target = tmp_path / "user-service" / "src" / "handlers.py"
        target.parent.mkdir(parents=True)
        target.write_text(_SOURCE, encoding="utf-8")

        ingestor = self._ingestor(
            target, {"user-service": str(tmp_path / "user-service")}
        )
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet("user-service.src.handlers.get_user")

        assert result.found is True
        assert result.source_code == _SOURCE

    @pytest.mark.asyncio
    async def test_allows_absolute_path_when_project_root_unknown(
        self, tmp_path: Path
    ) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()
        target = tmp_path / "user-service" / "src" / "handlers.py"
        target.parent.mkdir(parents=True)
        target.write_text(_SOURCE, encoding="utf-8")

        ingestor = self._ingestor(target, {})
        retriever = CodeRetriever(str(current_repo), ingestor)

        result = await retriever.find_code_snippet("user-service.src.handlers.get_user")

        assert result.found is True

    def test_function_source_rejects_path_outside_recorded_root(
        self, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere" / "handlers.py"
        outside.parent.mkdir(parents=True)
        outside.write_text(_SOURCE, encoding="utf-8")

        node_rows = [
            {
                "qualified_name": "user-service.src.handlers.get_user",
                "path": "definitely/not/here/handlers.py",
                "absolute_path": str(outside),
                "start_line": 1,
                "end_line": 2,
            }
        ]
        roots_rows = [
            {"name": "user-service", "root_path": str(tmp_path / "user-service")}
        ]
        ingestor = MagicMock()
        ingestor.fetch_all.side_effect = lambda query, params=None: (
            roots_rows if query == CYPHER_LIST_PROJECTS else node_rows
        )

        assert get_function_source_code(ingestor, node_id=1) is None


class TestDottedProjectNames:
    def test_dotted_project_name_still_bounds_reads(self, tmp_path: Path) -> None:
        # A custom project name may contain dots; the containment check must
        # match the longest known project prefix, not the first qn segment.
        from codebase_rag.utils.path_utils import absolute_path_within_project_root

        outside = tmp_path / "elsewhere" / "handlers.py"
        outside.parent.mkdir(parents=True)
        outside.write_text(_SOURCE, encoding="utf-8")

        roots = {"my.service": str(tmp_path / "my.service")}

        assert (
            absolute_path_within_project_root(
                "my.service.handlers.get_user", str(outside), roots
            )
            is False
        )

    def test_longest_project_prefix_wins(self, tmp_path: Path) -> None:
        from codebase_rag.utils.path_utils import absolute_path_within_project_root

        inside = tmp_path / "svc-v2" / "handlers.py"
        inside.parent.mkdir(parents=True)
        inside.write_text(_SOURCE, encoding="utf-8")

        roots = {
            "svc": str(tmp_path / "svc"),
            "svc.v2": str(tmp_path / "svc-v2"),
        }

        assert (
            absolute_path_within_project_root(
                "svc.v2.handlers.get_user", str(inside), roots
            )
            is True
        )


class TestRootsCaching:
    @pytest.mark.asyncio
    async def test_roots_fetched_once_per_retriever(self, tmp_path: Path) -> None:
        current_repo = tmp_path / "order-service"
        current_repo.mkdir()
        target = tmp_path / "user-service" / "src" / "handlers.py"
        target.parent.mkdir(parents=True)
        target.write_text(_SOURCE, encoding="utf-8")

        node_rows = [
            {
                "name": "get_user",
                "path": "src/handlers.py",
                "absolute_path": str(target),
                "start": 1,
                "end": 2,
                "docstring": None,
            }
        ]
        roots_rows = [
            {"name": "user-service", "root_path": str(tmp_path / "user-service")}
        ]
        ingestor = MagicMock()
        ingestor.fetch_all.side_effect = lambda query, params=None: (
            roots_rows if query == CYPHER_LIST_PROJECTS else node_rows
        )
        retriever = CodeRetriever(str(current_repo), ingestor)

        for _ in range(3):
            result = await retriever.find_code_snippet(
                "user-service.src.handlers.get_user"
            )
            assert result.found is True

        roots_calls = [
            c
            for c in ingestor.fetch_all.call_args_list
            if c.args[0] == CYPHER_LIST_PROJECTS
        ]
        assert len(roots_calls) == 1
