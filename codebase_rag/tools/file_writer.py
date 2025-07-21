from pathlib import Path

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import Tool


class FileCreationResult(BaseModel):
    """Data model for file creation results."""

    file_path: str
    success: bool = True
    error_message: str | None = None


class FileWriter:
    """Service to write file content to the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(f"FileWriter initialized with root: {self.project_root}")

    async def create_file(self, file_path: str, content: str) -> FileCreationResult:
        """Creates or overwrites a file with the given content."""
        logger.info(f"[FileWriter] Creating file: {file_path}")
        try:
            # Resolve the path to prevent traversal attacks
            full_path = (self.project_root / file_path).resolve()

            # Security check: Ensure the resolved path is within the project root
            full_path.relative_to(self.project_root)

            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
            logger.info(
                f"[FileWriter] Successfully wrote {len(content)} characters to {file_path}"
            )
            return FileCreationResult(file_path=file_path)
        except ValueError:
            err_msg = f"Security risk: Attempted to create file outside of project root: {file_path}"
            logger.error(err_msg)
            return FileCreationResult(
                file_path=file_path, success=False, error_message=err_msg
            )
        except Exception as e:
            err_msg = f"Error creating file {file_path}: {e}"
            logger.error(err_msg)
            return FileCreationResult(
                file_path=file_path, success=False, error_message=err_msg
            )


def create_file_writer_tool(file_writer: FileWriter) -> Tool:
    """Factory function to create the file writer tool."""

    async def create_new_file(file_path: str, content: str) -> FileCreationResult:
        """
        Creates a new file with the specified content.

        IMPORTANT: Before using this tool, you MUST check if the file already exists using
        the file reader or directory listing tools. If the file exists, use edit_existing_file
        instead to preserve existing content and show diffs.

        If the file already exists, it will be completely overwritten WITHOUT showing any diff.
        Use this ONLY for creating entirely new files, not for modifying existing ones.
        For modifying existing files with diff preview, use edit_existing_file instead.
        """
        return await file_writer.create_file(file_path, content)

    return Tool(
        function=create_new_file,
        description="Creates a new file with content. IMPORTANT: Check file existence first! Overwrites completely WITHOUT showing diff. Use only for new files, not existing file modifications.",
    )
