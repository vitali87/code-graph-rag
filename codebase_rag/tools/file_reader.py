from pathlib import Path
from pydantic import BaseModel
from pydantic_ai import Tool, RunContext
from loguru import logger


class FileContent(BaseModel):
    """Data model for file content results."""

    file_path: str
    content: str
    found: bool = True
    error_message: str | None = None


class FileReader:
    """Service to read file content from the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(f"FileReader initialized with root: {self.project_root}")

    async def read_file(self, file_path: str) -> FileContent:
        """Reads the content of a file given its path relative to the project root."""
        logger.info(f"[FileReader] Reading file: {file_path}")
        try:
            full_path = self.project_root / file_path
            if not full_path.is_file():
                err_msg = f"File not found at path: {full_path}"
                logger.warning(err_msg)
                return FileContent(
                    file_path=file_path, content="", found=False, error_message=err_msg
                )

            content = full_path.read_text(encoding="utf-8")
            logger.info(
                f"[FileReader] Successfully read {len(content)} characters from {file_path}"
            )
            return FileContent(file_path=file_path, content=content)

        except Exception as e:
            err_msg = f"Error reading file {file_path}: {e}"
            logger.error(err_msg)
            return FileContent(
                file_path=file_path, content="", found=False, error_message=err_msg
            )


def create_file_reader_tool(file_reader: FileReader) -> Tool:
    """Factory function to create the file reader tool."""

    async def read_file_content(ctx: RunContext, file_path: str) -> FileContent:
        """
        Reads the full content of a specified file (e.g., README.md, pyproject.toml).
        Use this to understand project goals, configuration, or non-code assets.
        The 'file_path' can be found from the 'path' property of nodes returned by the graph query tool.
        """
        return await file_reader.read_file(file_path)

    return Tool(
        function=read_file_content,
        description="Reads the entire content of a specific file from the codebase. Best for non-Python files like READMEs or configuration files.",
    )
