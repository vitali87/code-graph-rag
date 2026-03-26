from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai import Tool

from codebase_rag.constants import Provider
from codebase_rag.tools.document_analyzer import (
    DocumentAnalyzer,
    _NotSupportedClient,
    create_document_analyzer_tool,
)


@pytest.fixture
def temp_project_root(tmp_path: Path) -> Path:
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
    response.text = "Analysis result"
    client.models.generate_content.return_value = response
    return client


class TestNotSupportedClient:
    def test_raises_not_implemented_error(self) -> None:
        client = _NotSupportedClient()
        with pytest.raises(NotImplementedError):
            client.generate_content()

    def test_any_attribute_raises_error(self) -> None:
        client = _NotSupportedClient()
        with pytest.raises(NotImplementedError):
            client.any_method()


class TestDocumentAnalyzerInit:
    def test_init_resolves_project_root(
        self, temp_project_root: Path, mock_settings: MagicMock
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch("codebase_rag.tools.document_analyzer.genai.Client"):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                assert analyzer.project_root == temp_project_root.resolve()

    def test_init_with_google_api_provider(
        self, temp_project_root: Path, mock_settings: MagicMock
    ) -> None:
        mock_settings.active_orchestrator_config.provider = Provider.GOOGLE
        mock_settings.active_orchestrator_config.provider_type = "api"
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client"
            ) as mock_client:
                DocumentAnalyzer(str(temp_project_root))
                mock_client.assert_called_once_with(api_key="test-api-key")

    def test_init_with_non_google_provider(
        self, temp_project_root: Path, mock_settings: MagicMock
    ) -> None:
        mock_settings.active_orchestrator_config.provider = "anthropic"
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            assert isinstance(analyzer.client, _NotSupportedClient)


class TestDocumentAnalyzerAnalyze:
    def test_analyze_returns_error_for_unsupported_provider(
        self, temp_project_root: Path, mock_settings: MagicMock
    ) -> None:
        mock_settings.active_orchestrator_config.provider = "anthropic"
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            analyzer = DocumentAnalyzer(str(temp_project_root))
            result = analyzer.analyze("test.pdf", "What is this?")
            assert "Error:" in result
            assert "not supported" in result.lower()

    def test_analyze_file_not_found(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("nonexistent.pdf", "What is this?")
                assert "Error:" in result
                assert "not found" in result.lower()

    def test_analyze_security_path_traversal(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("../../../etc/passwd", "What is this?")
                assert "security" in result.lower()

    def test_analyze_existing_file_returns_response(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("test.txt", "What is this?")
                assert result == "Analysis result"

    def test_analyze_with_absolute_path(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze(str(test_file), "What is this?")
                assert result == "Analysis result"

    def test_analyze_handles_no_text_response(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        response = MagicMock()
        response.text = None
        response.candidates = None
        mock_client.models.generate_content.return_value = response

        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("test.txt", "What is this?")
                assert "no" in result.lower() and "content" in result.lower()

    def test_analyze_extracts_from_candidates(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
    ) -> None:
        mock_client = MagicMock()
        response = MagicMock()
        response.text = None

        candidate = MagicMock()
        part = MagicMock()
        part.text = "Candidate text"
        candidate.content.parts = [part]
        response.candidates = [candidate]
        mock_client.models.generate_content.return_value = response

        test_file = temp_project_root / "test.txt"
        test_file.write_text("Test content", encoding="utf-8")
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                result = analyzer.analyze("test.txt", "What is this?")
                assert result == "Candidate text"


class TestCreateDocumentAnalyzerTool:
    def test_creates_tool_instance(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                analyzer = DocumentAnalyzer(str(temp_project_root))
                tool = create_document_analyzer_tool(analyzer)
                assert isinstance(tool, Tool)

    def test_tool_has_description(
        self,
        temp_project_root: Path,
        mock_settings: MagicMock,
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
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
        mock_genai_client: MagicMock,
    ) -> None:
        with patch("codebase_rag.tools.document_analyzer.settings", mock_settings):
            with patch(
                "codebase_rag.tools.document_analyzer.genai.Client",
                return_value=mock_genai_client,
            ):
                from codebase_rag.tools.tool_descriptions import AgenticToolName

                analyzer = DocumentAnalyzer(str(temp_project_root))
                tool = create_document_analyzer_tool(analyzer)
                assert tool.name == AgenticToolName.ANALYZE_DOCUMENT
