from pathlib import Path

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import RunContext

from ..deps import RAGDeps


class FileReadResult(BaseModel):
    file_path: str
    content: str | None = None
    error_message: str | None = None


class FileReader:
    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self.binary_extensions = {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".ico",
            ".tiff",
            ".webp",
        }
        logger.info(f"FileReader initialized with root: {self.project_root}")

    async def read_file(self, file_path: str) -> FileReadResult:
        logger.info(f"[FileReader] Attempting to read file: {file_path}")
        try:
            full_path = (self.project_root / file_path).resolve()
            try:
                full_path.relative_to(self.project_root.resolve())
            except ValueError:
                return FileReadResult(
                    file_path=file_path,
                    error_message="Security risk: Attempted to read file outside of project root.",
                )

            if not str(full_path).startswith(str(self.project_root.resolve())):
                return FileReadResult(
                    file_path=file_path,
                    error_message="Security risk: Attempted to read file outside of project root.",
                )

            if not full_path.is_file():
                return FileReadResult(
                    file_path=file_path, error_message="File not found."
                )

            if full_path.suffix.lower() in self.binary_extensions:
                error_msg = f"File '{file_path}' is a binary file. Use the 'analyze_document' tool for this file type."
                logger.warning(f"[FileReader] {error_msg}")
                return FileReadResult(file_path=file_path, error_message=error_msg)

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


async def read_file_content(ctx: RunContext[RAGDeps], file_path: str) -> str:
    """
    Reads the content of a specified text-based file (e.g., source code, README.md, config files).
    This tool should NOT be used for binary files like PDFs or images. For those, use the 'analyze_document' tool.
    """
    result = await ctx.deps.file_reader.read_file(file_path)
    if result.error_message:
        return f"Error: {result.error_message}"
    return result.content or ""
