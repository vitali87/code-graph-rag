import mimetypes
import shutil
import uuid
from pathlib import Path

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from loguru import logger
from pydantic_ai import Tool

from ..config import settings


class _NotSupportedClient:
    """Placeholder client that raises NotImplementedError for unsupported providers."""

    def __getattr__(self, name: str) -> None:
        raise NotImplementedError(
            "DocumentAnalyzer does not support the 'local' LLM provider."
        )


class DocumentAnalyzer:
    """
    A tool to perform multimodal analysis on documents like PDFs
    by making a direct call to the Gemini API.
    """

    def __init__(self, project_root: str) -> None:
        self.project_root = Path(project_root).resolve()

        orchestrator_config = settings.active_orchestrator_config
        orchestrator_provider = orchestrator_config.provider

        if orchestrator_provider == "google":
            if orchestrator_config.provider_type == "vertex":
                self.client = genai.Client(
                    project=orchestrator_config.project_id,
                    location=orchestrator_config.region,
                )
            else:  # gla provider (default)
                self.client = genai.Client(api_key=orchestrator_config.api_key)
        else:
            self.client = _NotSupportedClient()

        logger.info(f"DocumentAnalyzer initialized with root: {self.project_root}")

    def analyze(self, file_path: str, question: str) -> str:
        """
        Reads a document (e.g., PDF), sends it to the Gemini multimodal endpoint
        with a specific question, and returns the model's analysis.
        """
        logger.info(
            f"[DocumentAnalyzer] Analyzing '{file_path}' with question: '{question}'"
        )
        if isinstance(self.client, _NotSupportedClient):
            return "Error: Document analysis is not supported for the current LLM provider."

        try:
            if Path(file_path).is_absolute():
                source_path = Path(file_path)
                if not source_path.is_file():
                    return f"Error: File not found at '{file_path}'."

                tmp_dir = self.project_root / ".tmp"
                tmp_dir.mkdir(exist_ok=True)

                tmp_file = tmp_dir / f"{uuid.uuid4()}-{source_path.name}"
                shutil.copy2(source_path, tmp_file)
                full_path = tmp_file
                logger.info(f"Copied external file to: {full_path}")
            else:
                full_path = (self.project_root / file_path).resolve()
                try:
                    full_path.relative_to(self.project_root.resolve())
                except ValueError:
                    return f"Security risk: file path {file_path} is outside the project root"

                if not str(full_path).startswith(str(self.project_root.resolve())):
                    return f"Security risk: file path {file_path} is outside the project root"

            if not full_path.is_file():
                return f"Error: File not found at '{file_path}'."

            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = (
                    "application/octet-stream"  # Default if type can't be guessed
                )

            file_bytes = full_path.read_bytes()

            prompt_parts = [
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                f"Based on the document provided, please answer the following question: {question}",
            ]

            orchestrator_config = settings.active_orchestrator_config
            response = self.client.models.generate_content(
                model=orchestrator_config.model_id, contents=prompt_parts
            )

            logger.success(f"Successfully received analysis for '{file_path}'.")

            if hasattr(response, "text") and response.text:
                return str(response.text)
            elif hasattr(response, "candidates") and response.candidates:
                for candidate in response.candidates:
                    if hasattr(candidate, "content") and candidate.content:
                        parts = candidate.content.parts
                        if parts and hasattr(parts[0], "text"):
                            return str(parts[0].text)
                return "No valid text found in response candidates."
            else:
                logger.warning(f"No text found in response: {response}")
                return "No text content received from the API."

        except ValueError as e:
            if "does not start with" in str(e):
                err_msg = f"Security risk: Attempted to access file outside of project root: {file_path}"
                logger.error(err_msg)
                return f"Error: {err_msg}"
            else:
                logger.error(f"[DocumentAnalyzer] API validation error: {e}")
                return f"Error: API validation failed: {e}"
        except ClientError as e:
            logger.error(f"Google GenAI API error for '{file_path}': {e}")
            if "Unable to process input image" in str(e):
                return "Error: Unable to process the image file. The image may be corrupted or in an unsupported format."
            return f"API error: {e}"
        except Exception as e:
            logger.error(
                f"Failed to analyze document '{file_path}': {e}", exc_info=True
            )
            return f"An error occurred during analysis: {e}"


def create_document_analyzer_tool(analyzer: DocumentAnalyzer) -> Tool:
    """Factory function to create the document analyzer tool."""

    def analyze_document(file_path: str, question: str) -> str:
        """
        Analyzes a document (like a PDF) to answer a specific question about its content.
        Use this tool when a user asks a question that requires understanding the content of a non-source-code file.

        Args:
            file_path: The path to the document file (e.g., 'path/to/book.pdf').
            question: The specific question to ask about the document's content.
        """
        try:
            result = analyzer.analyze(file_path, question)
            logger.debug(
                f"[analyze_document] Result type: {type(result)}, content: {result[:100] if result else 'None'}..."
            )
            return result
        except Exception as e:
            logger.error(
                f"[analyze_document] Exception during analysis: {e}", exc_info=True
            )
            if str(e).startswith("Error:") or str(e).startswith("API error:"):
                return str(e)
            return f"Error during document analysis: {e}"

    return Tool(
        function=analyze_document,
        name="analyze_document",
        description="Analyzes documents (PDFs, images) to answer questions about their content.",
    )
