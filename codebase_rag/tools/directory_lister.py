import os
from pathlib import Path

from loguru import logger
from pydantic_ai import RunContext

from ..deps import RAGDeps


class DirectoryLister:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def list_directory_contents(self, directory_path: str) -> str:
        target_path = self._get_safe_path(directory_path)
        logger.info(f"Listing contents of directory: {target_path}")

        try:
            if not target_path.is_dir():
                return f"Error: '{directory_path}' is not a valid directory."

            if contents := os.listdir(target_path):
                return "\n".join(contents)
            else:
                return f"The directory '{directory_path}' is empty."

        except Exception as e:
            logger.error(f"Error listing directory {directory_path}: {e}")
            return f"Error: Could not list contents of '{directory_path}'."

    def _get_safe_path(self, file_path: str) -> Path:
        if Path(file_path).is_absolute():
            safe_path = Path(file_path).resolve()
        else:
            safe_path = (self.project_root / file_path).resolve()

        try:
            safe_path.relative_to(self.project_root.resolve())
        except ValueError as e:
            raise PermissionError(
                "Access denied: Cannot access files outside the project root."
            ) from e

        if not str(safe_path).startswith(str(self.project_root.resolve())):
            raise PermissionError(
                "Access denied: Cannot access files outside the project root."
            )

        return safe_path


def list_directory(ctx: RunContext[RAGDeps], directory_path: str) -> str:
    """
    Lists the contents of a directory to explore the codebase.
    """
    return ctx.deps.directory_lister.list_directory_contents(directory_path)
