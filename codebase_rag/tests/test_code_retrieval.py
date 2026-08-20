from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from codebase_rag.schemas import CodeSnippet
from codebase_rag.tools.code_retrieval import CodeRetriever, create_code_retrieval_tool


class TestCodeRetrieverInit:
    def test_init_resolves_project_root(self) -> None:
        mock_ingestor = MagicMock()

        retriever = CodeRetriever("/tmp/project", mock_ingestor)

        assert retriever.project_root == Path("/tmp/project").resolve()

    def test_init_stores_ingestor(self) -> None:
        mock_ingestor = MagicMock()

        retriever = CodeRetriever("/tmp/project", mock_ingestor)

        assert retriever.ingestor is mock_ingestor


class TestFindCodeSnippet:
    @pytest.fixture
    def mock_ingestor(self) -> MagicMock:
        return MagicMock()

    @pytest.fixture
    def retriever(self, mock_ingestor: MagicMock) -> CodeRetriever:
        return CodeRetriever("/tmp/project", mock_ingestor)

    @pytest.mark.asyncio
    async def test_returns_not_found_when_no_results(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.return_value = []

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.error_message == "Entity not found in graph."
        assert result.qualified_name == "module.func"

    @pytest.mark.asyncio
    async def test_returns_not_found_when_missing_path(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"path": None, "start": 1, "end": 10, "name": "func"}
        ]

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_path", ["", "   "])
    async def test_returns_not_found_when_path_is_blank(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock, file_path: str
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"path": file_path, "start": 1, "end": 10, "name": "func"}
        ]

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file_path", [123, {"path": "module.py"}])
    async def test_returns_not_found_when_path_is_not_a_string(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock, file_path: object
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"path": file_path, "start": 1, "end": 1, "name": "func"}
        ]

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.file_path == ""
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    async def test_returns_not_found_when_missing_start_line(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"path": "src/mod.py", "start": None, "end": 10, "name": "func"}
        ]

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False

    @pytest.mark.asyncio
    async def test_returns_not_found_when_missing_end_line(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.return_value = [
            {"path": "src/mod.py", "start": 1, "end": None, "name": "func"}
        ]

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("start_line", "end_line"),
        [(-1, 2), (2, -1), (3, 2), (True, 2), (1, False)],
    )
    async def test_returns_not_found_for_invalid_line_range(
        self,
        mock_ingestor: MagicMock,
        tmp_path: Path,
        start_line: int,
        end_line: int,
    ) -> None:
        source_file = tmp_path / "module.py"
        source_file.write_text("line1\nline2\n", encoding="utf-8")
        mock_ingestor.fetch_all.return_value = [
            {
                "path": "module.py",
                "start": start_line,
                "end": end_line,
                "name": "func",
            }
        ]
        retriever = CodeRetriever(str(tmp_path), mock_ingestor)

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.source_code == ""
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    async def test_returns_not_found_when_line_range_exceeds_file(
        self, mock_ingestor: MagicMock, tmp_path: Path
    ) -> None:
        source_file = tmp_path / "module.py"
        source_file.write_text("line1\nline2\n", encoding="utf-8")
        mock_ingestor.fetch_all.return_value = [
            {"path": "module.py", "start": 1, "end": 3, "name": "func"}
        ]
        retriever = CodeRetriever(str(tmp_path), mock_ingestor)

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.source_code == ""
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path_kind", ["absolute", "traversal"])
    async def test_returns_not_found_when_path_escapes_project_root(
        self, mock_ingestor: MagicMock, tmp_path: Path, path_kind: str
    ) -> None:
        project_root = tmp_path / "project"
        project_root.mkdir()
        outside_file = tmp_path / "outside.py"
        outside_file.write_text("secret\n", encoding="utf-8")
        file_path = str(outside_file) if path_kind == "absolute" else "../outside.py"
        mock_ingestor.fetch_all.return_value = [
            {"path": file_path, "start": 1, "end": 1, "name": "func"}
        ]
        retriever = CodeRetriever(str(project_root), mock_ingestor)

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.source_code == ""
        assert result.error_message is not None
        assert "missing location data" in result.error_message

    @pytest.mark.asyncio
    async def test_handles_ingestor_error(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.side_effect = RuntimeError("Database error")

        result = await retriever.find_code_snippet("module.func")

        assert result.found is False
        assert result.error_message is not None
        assert "Database error" in result.error_message

    @pytest.mark.asyncio
    async def test_uses_cypher_query_constant(
        self, retriever: CodeRetriever, mock_ingestor: MagicMock
    ) -> None:
        mock_ingestor.fetch_all.return_value = []

        await retriever.find_code_snippet("module.func")

        call_args = mock_ingestor.fetch_all.call_args
        query = call_args[0][0]
        params = call_args[0][1]

        assert "qualified_name" in query
        assert "start_line" in query or "start" in query
        assert "end_line" in query or "end" in query
        assert params == {"qn": "module.func"}


class TestCreateCodeRetrievalTool:
    def test_creates_tool_with_description(self) -> None:
        mock_retriever = MagicMock(spec=CodeRetriever)

        tool = create_code_retrieval_tool(mock_retriever)

        assert tool is not None
        assert tool.description is not None
        assert "qualified name" in tool.description.lower()

    @pytest.mark.asyncio
    async def test_tool_calls_retriever(self) -> None:
        mock_retriever = MagicMock(spec=CodeRetriever)
        mock_retriever.find_code_snippet = AsyncMock(
            return_value=CodeSnippet(
                qualified_name="test.func",
                source_code="def func(): pass",
                file_path="test.py",
                line_start=1,
                line_end=1,
            )
        )

        tool = create_code_retrieval_tool(mock_retriever)
        result = await tool.function("test.func")

        mock_retriever.find_code_snippet.assert_called_once_with("test.func")
        assert result.qualified_name == "test.func"
