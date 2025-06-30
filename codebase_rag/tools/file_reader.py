from pathlib import Path
from pydantic import BaseModel
from pydantic_ai import Tool
from loguru import logger


class FileReadResult(BaseModel):
    """Data model for file read results."""

    file_path: str
    content: str | None = None
    error_message: str | None = None


class FileReader:
    """Service to read file content from the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        # Define extensions that should be treated as binary and not read by this tool
        self.binary_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".webp"}
        logger.info(f"FileReader initialized with root: {self.project_root}")

    async def read_file(self, file_path: str) -> FileReadResult:
        """Reads and returns the content of a text-based file."""
        logger.info(f"[FileReader] Attempting to read file: {file_path}")
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root) # Security check

            if not full_path.is_file():
                return FileReadResult(file_path=file_path, error_message="File not found.")

            # Check if the file has a binary extension
            if full_path.suffix.lower() in self.binary_extensions:
                error_msg = f"File '{file_path}' is a binary file. Use the 'analyze_document' tool for this file type."
                logger.warning(f"[FileReader] {error_msg}")
                return FileReadResult(file_path=file_path, error_message=error_msg)

            # Proceed with reading as a text file
            try:
                content = full_path.read_text(encoding="utf-8")
                logger.info(f"[FileReader] Successfully read text from {file_path}")
                return FileReadResult(file_path=file_path, content=content)
            except UnicodeDecodeError:
                error_msg = f"File '{file_path}' could not be read as text. It may be a binary file. If it is a document (e.g., PDF), use the 'analyze_document' tool."
                logger.warning(f"[FileReader] {error_msg}")
                return FileReadResult(file_path=file_path, error_message=error_msg)

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
        Reads the content of a specified text-based file (e.g., source code, README.md, config files).
        This tool should NOT be used for binary files like PDFs or images. For those, use the 'analyze_document' tool.
        """
        result = await file_reader.read_file(file_path)
        if result.error_message:
            return f"Error: {result.error_message}"
        return result.content or ""

    return Tool(
        function=read_file_content,
        description="Reads the content of text-based files. For documents like PDFs or images, use the 'analyze_document' tool instead.",
    )
