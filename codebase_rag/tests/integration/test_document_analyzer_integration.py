from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.constants import Provider
from codebase_rag.tools.document_analyzer import (
    DocumentAnalyzer,
    create_document_analyzer_tool,
)


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
    settings.active_orchestrator_config.provider = Provider.GOOGLE
    settings.active_orchestrator_config.provider_type = "api"
    settings.active_orchestrator_config.api_key = "test-api-key"
    settings.active_orchestrator_config.model_id = "gemini-1.5-flash"
    return settings


@pytest.fixture
def mock_genai_client() -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.text = "This is an analysis of the document."
    client.models.generate_content.return_value = response
    return client


@pytest.fixture
def analyzer_with_mock(
    temp_test_repo: Path,
    mock_settings: MagicMock,
    mock_genai_client: MagicMock,
) -> DocumentAnalyzer:
    with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
        with patch(
            "codebase_rag.tools.document_analyzer.genai.Client",
            return_value=mock_genai_client,
        ):
            return DocumentAnalyzer(str(temp_test_repo))


class TestDocumentAnalyzerIntegration:
    def test_analyze_text_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_genai_client: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("readme.txt", "What is this file about?")
        assert "analysis" in result.lower()
        mock_genai_client.models.generate_content.assert_called_once()

    def test_analyze_code_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_genai_client: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("code.py", "What does this code do?")
        assert "analysis" in result.lower()

    def test_analyze_json_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_genai_client: MagicMock,
    ) -> None:
        result = analyzer_with_mock.analyze("data.json", "What data is in this file?")
        assert "analysis" in result.lower()

    def test_analyze_nested_file(
        self,
        analyzer_with_mock: DocumentAnalyzer,
        mock_genai_client: MagicMock,
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
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
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
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
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
        mock_settings = MagicMock()
        mock_settings.active_orchestrator_config.provider = "anthropic"
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_test_repo))
            result = analyzer.analyze("readme.txt", "What is this?")
            assert "not supported" in result.lower()


class TestDocumentAnalyzerResponseHandling:
    def test_handles_response_with_candidates(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        response = MagicMock()
        response.text = None
        candidate = MagicMock()
        part = MagicMock()
        part.text = "Analysis from candidate"
        candidate.content.parts = [part]
        response.candidates = [candidate]
        mock_client.models.generate_content.return_value = response

        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_test_repo))
                result = analyzer.analyze("readme.txt", "What is this?")
                assert result == "Analysis from candidate"

    def test_handles_empty_response(
        self,
        temp_test_repo: Path,
        mock_settings: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        response = MagicMock()
        response.text = None
        response.candidates = None
        mock_client.models.generate_content.return_value = response

        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_test_repo))
                result = analyzer.analyze("readme.txt", "What is this?")
                assert "no" in result.lower() and "content" in result.lower()
