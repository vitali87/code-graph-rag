import os
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool


class DirectoryLister:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def list_directory_contents(self, directory_path: str) -> str:
        """
        Lists the contents of a specified directory.
        """
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
        """
        Resolves the file path relative to the root and ensures it's within
        the project directory.
        """
        # Accommodate both relative and absolute paths from the agent
        if Path(file_path).is_absolute():
            # If absolute, it should still be within the root path
            safe_path = Path(file_path).resolve()
        else:
            # If relative, resolve it against the root path
            safe_path = (self.project_root / file_path).resolve()

        # Enhanced security check to prevent directory traversal attacks
        try:
            safe_path.relative_to(self.project_root.resolve())
        except ValueError as e:
            raise PermissionError(
                "Access denied: Cannot access files outside the project root."
            ) from e

        # Additional check for symlinks that might bypass relative_to
        if not str(safe_path).startswith(str(self.project_root.resolve())):
            raise PermissionError(
                "Access denied: Cannot access files outside the project root."
            )

        return safe_path


def create_directory_lister_tool(directory_lister: DirectoryLister) -> Tool:
    return Tool(
        function=directory_lister.list_directory_contents,
        description="Lists the contents of a directory to explore the codebase.",
    )
