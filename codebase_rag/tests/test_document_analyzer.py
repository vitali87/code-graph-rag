from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Tool

from codebase_rag.constants import Provider
from codebase_rag.tools.document_analyzer import (
    DocumentAnalyzer,
    create_document_analyzer_tool,
)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.active_orchestrator_config.provider = Provider.OPENAI
    settings.active_orchestrator_config.provider_type = "api"
    settings.active_orchestrator_config.api_key = "test-api-key"
    settings.active_orchestrator_config.model_id = "gpt-4o"
    settings.active_orchestrator_config.endpoint = None
    settings.active_orchestrator_config.project_id = None
    settings.active_orchestrator_config.region = None
    settings.active_orchestrator_config.provider_type = None
    settings.active_orchestrator_config.thinking_budget = None
    settings.active_orchestrator_config.service_account_file = None
    return settings


@pytest.fixture
def mock_agent_run() -> MagicMock:
    """Mock the Agent.run_sync method."""
    with patch("codebase_rag.tools.document_analyzer.Agent.run_sync") as mock_run:
        result = MagicMock()
        result.data = "Analysis result"
        mock_run.return_value = result
        yield mock_run


class TestDocumentAnalyzerInit:
    def test_init_resolves_project_root(
        self, temp_project_root: Path, mock_settings: MagicMock
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            assert analyzer.project_root == temp_project_root.resolve()


class TestDocumentAnalyzerAnalyze:
    def test_analyze_file_not_found(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            result = analyzer.analyze("nonexistent.pdf", "What is this?")
            assert "Error:" in result
            assert "not found" in result.lower()

    def test_analyze_security_path_traversal(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            result = analyzer.analyze("../../../etc/passwd", "What is this?")
            assert "security" in result.lower()

    def test_analyze_existing_file_returns_response(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_agent_run: MagicMock,
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")

        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            result = analyzer.analyze("test.txt", "What is this?")
            assert result == "Analysis result"
            mock_agent_run.assert_called_once()

    def test_analyze_with_absolute_path(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_agent_run: MagicMock,
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")

        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            result = analyzer.analyze(str(test_file), "What is this?")
            assert result == "Analysis result"
            mock_agent_run.assert_called_once()

    def test_analyze_handles_agent_error(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")

        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.Agent.run_sync",
                side_effect=Exception("API Error"),
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("test.txt", "What is this?")
                assert "failed" in result.lower() or "error" in result.lower()


class TestCreateDocumentAnalyzerTool:
    def test_creates_tool_instance(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            tool = create_document_analyzer_tool(analyzer)
            assert isinstance(tool, Tool)

    def test_tool_has_description(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            tool = create_document_analyzer_tool(analyzer)
            assert tool.description is not None
            assert (
                "document" in tool.description.lower()
                or "pdf" in tool.description.lower()
            )

    def test_tool_has_correct_name(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            from codebase_rag.tools.tool_descriptions import AgenticToolName

            analyzer = DocumentAnalyzer(str(temp_project_root))
            tool = create_document_analyzer_tool(analyzer)
            assert tool.name == AgenticToolName.ANALYZE_DOCUMENT
