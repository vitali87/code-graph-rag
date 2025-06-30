from pathlib import Path
from pydantic import BaseModel
from pydantic_ai import Tool
from loguru import logger
import base64


class FileReadResult(BaseModel):
    """Data model for file read results."""

    file_path: str
    content: str | None = None
    error_message: str | None = None
    is_binary: bool = False


class FileReader:
    """Service to read file content from the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(f"FileReader initialized with root: {self.project_root}")

    async def read_file(self, file_path: str) -> FileReadResult:
        """Reads and returns the content of a file."""
        logger.info(f"[FileReader] Reading file: {file_path}")
        try:
            # Resolve the path to prevent traversal attacks
            full_path = (self.project_root / file_path).resolve()

            # Security check: Ensure the resolved path is within the project root
            full_path.relative_to(self.project_root)

            if not full_path.is_file():
                return FileReadResult(
                    file_path=file_path, error_message="File not found."
                )

            try:
                # Try reading as text first
                logger.info(f"Reading text file {file_path}.")
                content = full_path.read_text(encoding="utf-8")
                return FileReadResult(file_path=file_path, content=content)
            except UnicodeDecodeError:
                # If that fails, treat it as a binary file
                logger.info(f"File {file_path} is binary, encoding as base64.")
                content_bytes = full_path.read_bytes()
                content_b64 = base64.b64encode(content_bytes).decode("utf-8")
                return FileReadResult(
                    file_path=file_path, content=content_b64, is_binary=True
                )
        except ValueError:
            return FileReadResult(
                file_path=file_path,
                error_message="Security risk: Attempted to read file outside of project root.",
            )
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return FileReadResult(
                file_path=file_path, error_message=f"An unexpected error occurred: {e}"
            )


def create_file_reader_tool(file_reader: FileReader) -> Tool:
    """Factory function to create the file reader tool."""

    async def read_file_content(file_path: str) -> str:
        """
        Reads and returns the content of any file from the codebase. It handles both text and binary files.
        - For text files, it returns the content as a string.
        - For binary files (e.g., images), it returns a base64-encoded string.
        """
        result = await file_reader.read_file(file_path)
        if result.error_message:
            return f"Error: {result.error_message}"
        return result.content or ""

    return Tool(
        function=read_file_content,
        description="Reads and returns the content of a specified file.\n- For text files, it returns the content as a string.\n- For binary files, it returns a base64-encoded string.",
    )
