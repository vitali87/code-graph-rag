from pathlib import Path

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import RunContext, Tool


class EditResult(BaseModel):
    """Data model for file edit results."""

    file_path: str
    success: bool
    error_message: str | None = None


class FileEditor:
    """Service to edit files in the filesystem."""

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(f"FileEditor initialized with root: {self.project_root}")

    async def edit_file(self, file_path: str, new_content: str) -> EditResult:
        """Overwrites the content of a file given its path and new content."""
        logger.info(f"[FileEditor] Editing file: {file_path}")
        try:
            # Resolve the path to prevent traversal attacks
            full_path = (self.project_root / file_path).resolve()

            # Security check: Ensure the resolved path is within the project root
            full_path.relative_to(self.project_root)

            if not full_path.is_file():
                err_msg = f"File not found at path: {full_path}"
                logger.warning(err_msg)
                return EditResult(
                    file_path=file_path, success=False, error_message=err_msg
                )

            full_path.write_text(new_content, encoding="utf-8")
            logger.info(
                f"[FileEditor] Successfully wrote {len(new_content)} characters to {file_path}"
            )
            return EditResult(file_path=file_path, success=True)

        except ValueError:
            err_msg = (
                f"Security risk: Attempted to edit file outside of project root: {file_path}"
            )
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )
        except Exception as e:
            err_msg = f"Error writing to file {file_path}: {e}"
            logger.error(err_msg)
            return EditResult(
                file_path=file_path, success=False, error_message=err_msg
            )


def create_file_editor_tool(file_editor: FileEditor) -> Tool:
    """Factory function to create the file editor tool."""

    async def edit_existing_file(
        ctx: RunContext, file_path: str, new_content: str
    ) -> EditResult:
        """
        Overwrites the content of a specified file with new content.
        Use this to modify existing files. The 'file_path' can be found
        from the 'path' property of nodes returned by the graph query tool.
        """
        return await file_editor.edit_file(file_path, new_content)

    return Tool(
        function=edit_existing_file,
        description="Overwrites an existing file with new content. Use with caution.",
    )
