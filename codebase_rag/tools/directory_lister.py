from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from ..constants import (
    ERR_ACCESS_DENIED,
    ERR_DIRECTORY_EMPTY,
    ERR_DIRECTORY_INVALID,
    ERR_DIRECTORY_LIST_FAILED,
    LOG_DIR_LIST_ERROR,
    LOG_DIR_LISTING,
)


class DirectoryLister:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def list_directory_contents(self, directory_path: str) -> str:
        target_path = self._get_safe_path(directory_path)
        logger.info(LOG_DIR_LISTING.format(path=target_path))

        try:
            if not target_path.is_dir():
                return ERR_DIRECTORY_INVALID.format(path=directory_path)

            if contents := os.listdir(target_path):
                return "\n".join(contents)
            else:
                return ERR_DIRECTORY_EMPTY.format(path=directory_path)

        except Exception as e:
            logger.error(LOG_DIR_LIST_ERROR.format(path=directory_path, error=e))
            return ERR_DIRECTORY_LIST_FAILED.format(path=directory_path)

    def _get_safe_path(self, file_path: str) -> Path:
        if Path(file_path).is_absolute():
            safe_path = Path(file_path).resolve()
        else:
            safe_path = (self.project_root / file_path).resolve()

        try:
            safe_path.relative_to(self.project_root.resolve())
        except ValueError as e:
            raise PermissionError(ERR_ACCESS_DENIED) from e

        if not str(safe_path).startswith(str(self.project_root.resolve())):
            raise PermissionError(ERR_ACCESS_DENIED)

        return safe_path


def create_directory_lister_tool(directory_lister: DirectoryLister) -> Tool:
    return Tool(
        function=directory_lister.list_directory_contents,
        description="Lists the contents of a directory to explore the codebase.",
    )
