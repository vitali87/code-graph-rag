from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.tools.document_analyzer import (
    DocumentAnalyzer,
    create_document_analyzer_tool,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture
def temp_test_repo(tmp_path: Path) -> Path:
    (tmp_path / "readme.txt").write_text(
        "This is a README file.\nIt contains important information.",
        encoding="utf-8",
    )
    (tmp_path / "code.py").write_text(
        "def hello():\n    return 'Hello, World!'",
        encoding="utf-8",
    )
    (tmp_path / "data.json").write_text(
        '{"name": "test", "value": 42}',
        encoding="utf-8",
    )
    subdir = tmp_path / "docs"
    subdir.mkdir()
    (subdir / "manual.txt").write_text(
        "User Manual\n\n1. Getting Started\n2. Configuration",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def mock_settings() -> MagicMock:
    settings = MagicMock()
    settings.active_orchestrator_config.provider = "openai"
    settings.active_orchestrator_config.provider_type = "api"
    settings.active_orchestrator_config.api_key = "test-api-key"
    settings.active_orchestrator_config.model_id = "gpt-4o"
    return settings


@pytest.fixture
def mock_agent_run() -> MagicMock:
    with patch("codebase_rag.tools.document_analyzer.Agent") as mock_agent_cls:
        mock_instance = MagicMock()
        mock_result = MagicMock()
        mock_result.output = "This is an analysis of the document."
        mock_instance.run_sync.return_value = mock_result
        mock_agent_cls.return_value = mock_instance
        yield mock_instance.run_sync


@pytest.fixture
def analyzer_with_mock(
    temp_test_repo: Path,
    mock_settings: MagicMock,
    mock_agent_run: MagicMock,
) -> DocumentAnalyzer:
    with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
        with patch("codebase_rag.tools.document_analyzer._create_provider_model"):
            return DocumentAnalyzer(str(temp_test_repo))


class TestDocumentAnalyzerIntegration:
    def test_analyze_text_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_agent_run: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("readme.txt", "What is this file about?")
        assert "analysis" in result.lower()
        mock_agent_run.assert_called_once()

    def test_analyze_code_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_agent_run: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("code.py", "What does this code do?")
        assert "analysis" in result.lower()

    def test_analyze_json_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_agent_run: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("data.json", "What data is in this file?")
        assert "analysis" in result.lower()

    def test_analyze_nested_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_agent_run: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("docs/manual.txt", "Summarize this manual")
        assert "analysis" in result.lower()

    def test_analyze_nonexistent_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
    ) -> None:
        result = analyzer_with_mock.analyze("nonexistent.txt", "What is this?")
        assert "error" in result.lower()
        assert "not found" in result.lower()

    def test_analyze_path_traversal_blocked(
        self,
        analyzer_with_mock: DocumentAnalyzer,
    ) -> None:
        result = analyzer_with_mock.analyze("../../../etc/passwd", "What is this?")
        assert "security" in result.lower()


class TestDocumentAnalyzerToolIntegration:
    def test_tool_analyzes_file(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
        mock_agent_run: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch("codebase_rag.tools.document_analyzer._create_provider_model"):
                analyzer = DocumentAnalyzer(str(temp_test_repo))
                tool = create_document_analyzer_tool(analyzer)
                result = tool.function(
                    file_path="readme.txt",
                    question="What is in this file?",
                )
                assert "analysis" in result.lower()

    def test_tool_handles_error(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
        mock_agent_run: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch("codebase_rag.tools.document_analyzer._create_provider_model"):
                analyzer = DocumentAnalyzer(str(temp_test_repo))
                tool = create_document_analyzer_tool(analyzer)
                result = tool.function(
                    file_path="missing.txt",
                    question="What is this?",
                )
                assert "error" in result.lower()


class TestDocumentAnalyzerWithDifferentProviders:
    def test_unsupported_provider_returns_error(
        self,
        temp_test_repo: Path,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings"):
            with patch(
                "codebase_rag.tools.document_analyzer._create_provider_model",
                side_effect=ValueError("Provider error"),
            ):
                analyzer = DocumentAnalyzer(str(temp_test_repo))
                result = analyzer.analyze("readme.txt", "What is this?")
                assert "failed" in result.lower() or "error" in result.lower()


class TestDocumentAnalyzerResponseHandling:
    def test_handles_response_with_candidates(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch("codebase_rag.tools.document_analyzer._create_provider_model"):
                with patch(
                    "codebase_rag.tools.document_analyzer.Agent"
                ) as mock_agent_cls:
                    mock_instance = MagicMock()
                    mock_result = MagicMock()
                    mock_result.output = "Analysis from candidate"
                    mock_instance.run_sync.return_value = mock_result
                    mock_agent_cls.return_value = mock_instance

                    analyzer = DocumentAnalyzer(str(temp_test_repo))
                    result = analyzer.analyze("readme.txt", "What is this?")
                    assert result == "Analysis from candidate"

    def test_handles_empty_response(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch("codebase_rag.tools.document_analyzer._create_provider_model"):
                with patch(
                    "codebase_rag.tools.document_analyzer.Agent"
                ) as mock_agent_cls:
                    mock_instance = MagicMock()
                    mock_result = MagicMock()
                    mock_result.output = None
                    mock_instance.run_sync.return_value = mock_result
                    mock_agent_cls.return_value = mock_instance

                    analyzer = DocumentAnalyzer(str(temp_test_repo))
                    result = analyzer.analyze("readme.txt", "What is this?")
                    assert result in ("None", "")
