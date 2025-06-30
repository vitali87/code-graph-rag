# Create new file: codebase_rag/tools/document_analyzer.py

import os
from pathlib import Path
from pydantic_ai import Tool, RunContext
from loguru import logger

# We need the google-genai library for direct multimodal calls
try:
    from google import genai
    from google.genai import types
except ImportError as e:
    raise ImportError(
        "Please install google-genai for multimodal capabilities: `uv add google-genai`"
    ) from e

class DocumentAnalyzer:
    """
    A tool to perform multimodal analysis on documents like PDFs
    by making a direct call to the Gemini API.
    """
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in the environment.")

        self.client = genai.Client(api_key=api_key)
        logger.info(f"DocumentAnalyzer initialized with root: {self.project_root}")

    def analyze(self, file_path: str, question: str) -> str:
        """
        Reads a document (e.g., PDF), sends it to the Gemini multimodal endpoint
        with a specific question, and returns the model's analysis.
        """
        logger.info(f"[DocumentAnalyzer] Analyzing '{file_path}' with question: '{question}'")
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root) # Security check

            if not full_path.is_file():
                return f"Error: File not found at '{file_path}'."

            # Prepare the multimodal prompt - use simple format
            file_bytes = full_path.read_bytes()
            
            # Use the simpler format that the library expects
            prompt_parts = [
                types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"),
                f"Based on the document provided, please answer the following question: {question}"
            ]

            # Call the model and get the response
            response = self.client.models.generate_content(
                model='gemini-1.5-flash',
                contents=prompt_parts,
                config=types.GenerateContentConfig(
                    safety_settings=[
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                        types.SafetySetting(
                            category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                            threshold=types.HarmBlockThreshold.BLOCK_NONE
                        ),
                    ]
                )
            )

            logger.success(f"Successfully received analysis for '{file_path}'.")
            
            # Check if response has text content
            if hasattr(response, 'text') and response.text:
                return str(response.text)
            elif hasattr(response, 'candidates') and response.candidates:
                # Try to get text from candidates
                for candidate in response.candidates:
                    if hasattr(candidate, 'content') and candidate.content:
                        parts = candidate.content.parts
                        if parts and hasattr(parts[0], 'text'):
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
        except Exception as e:
            logger.error(f"Failed to analyze document '{file_path}': {e}", exc_info=True)
            return f"An error occurred during analysis: {e}"


def create_document_analyzer_tool(analyzer: DocumentAnalyzer) -> Tool:
    """Factory function to create the document analyzer tool."""
    
    # The context is not used here but required by the pydantic-ai Tool signature.
    def analyze_document(ctx: RunContext, file_path: str, question: str) -> str:
        """
        Analyzes a document (like a PDF) to answer a specific question about its content.
        Use this tool when a user asks a question that requires understanding the content of a non-source-code file.
        
        Args:
            file_path: The path to the document file (e.g., 'path/to/book.pdf').
            question: The specific question to ask about the document's content.
        """
        try:
            result = analyzer.analyze(file_path, question)
            logger.debug(f"[analyze_document] Result type: {type(result)}, content: {result[:100] if result else 'None'}...")
            return result
        except Exception as e:
            logger.error(f"[analyze_document] Exception during analysis: {e}", exc_info=True)
            return f"Error during document analysis: {e}"

    return Tool(
        function=analyze_document,
        name="analyze_document",
        description="Analyzes documents (PDFs, images) to answer questions about their content."
    )