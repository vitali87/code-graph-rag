import mimetypes
import shutil
import uuid
from pathlib import Path

from google import genai
from google.genai import types
from google.genai.errors import ClientError
from loguru import logger
from pydantic_ai import Tool

from ..config import detect_provider_from_model, settings


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

        # Initialize client based on the orchestrator model's provider
        # Note: Document analysis uses the orchestrator model since it's the main reasoning model
        orchestrator_model = settings.active_orchestrator_model
        orchestrator_provider = detect_provider_from_model(orchestrator_model)

        if orchestrator_provider == "gemini":
            if settings.GEMINI_PROVIDER == "gla":
                self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
            else:  # vertex provider
                # For Vertex AI, use service account authentication
                self.client = genai.Client(
                    project=settings.GCP_PROJECT_ID,
                    location=settings.GCP_REGION,
                    credentials_path=settings.GCP_SERVICE_ACCOUNT_FILE,
                )
        else:
            # Non-Gemini providers are not supported for document analysis yet.
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
        try:
            # Handle absolute paths by copying to .tmp folder
            if Path(file_path).is_absolute():
                source_path = Path(file_path)
                if not source_path.is_file():
                    return f"Error: File not found at '{file_path}'."

                # Create .tmp folder if it doesn't exist
                tmp_dir = self.project_root / ".tmp"
                tmp_dir.mkdir(exist_ok=True)

                # Copy file to .tmp with a unique filename to avoid collisions
                tmp_file = tmp_dir / f"{uuid.uuid4()}-{source_path.name}"
                shutil.copy2(source_path, tmp_file)
                full_path = tmp_file
                logger.info(f"Copied external file to: {full_path}")
            else:
                # Handle relative paths as before
                full_path = (self.project_root / file_path).resolve()
                full_path.relative_to(self.project_root)  # Security check

            if not full_path.is_file():
                return f"Error: File not found at '{file_path}'."

            # Determine mime type dynamically
            mime_type, _ = mimetypes.guess_type(full_path)
            if not mime_type:
                mime_type = (
                    "application/octet-stream"  # Default if type can't be guessed
                )

            # Prepare the multimodal prompt
            file_bytes = full_path.read_bytes()

            # Use the simpler format that the library expects
            prompt_parts = [
                types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                f"Based on the document provided, please answer the following question: {question}",
            ]

            # Call the model and get the response
            response = self.client.models.generate_content(
                model=settings.GEMINI_MODEL_ID, contents=prompt_parts
            )

            logger.success(f"Successfully received analysis for '{file_path}'.")

            # Check if response has text content
            if hasattr(response, "text") and response.text:
                return str(response.text)
            elif hasattr(response, "candidates") and response.candidates:
                # Try to get text from candidates
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
            # Check if this is a security-related ValueError (from relative_to)
            if "does not start with" in str(e):
                err_msg = f"Security risk: Attempted to access file outside of project root: {file_path}"
                logger.error(err_msg)
                return f"Error: {err_msg}"
            else:
                # API-related ValueError
                logger.error(f"[DocumentAnalyzer] API validation error: {e}")
                return f"Error: API validation failed: {e}"
        except ClientError as e:
            # Handle Google GenAI specific errors
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
