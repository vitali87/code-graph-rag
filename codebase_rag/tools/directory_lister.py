from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import exceptions as ex
from .. import logs as ls
from .. import tool_errors as te
from . import tool_descriptions as td


class DirectoryLister:
    def __init__(self, project_root: str, allowed_roots: frozenset[Path] | None = None):
        self.project_root = Path(project_root).resolve()
        self.allowed_roots = frozenset(
            {self.project_root} | ({root.resolve() for root in allowed_roots or []})
        )

    def list_directory_contents(self, directory_path: str) -> str:
        target_path = self._get_safe_path(directory_path)
        logger.info(ls.DIR_LISTING.format(path=target_path))

        try:
            if not target_path.is_dir():
                return te.DIRECTORY_INVALID.format(path=directory_path)

            if contents := os.listdir(target_path):
                return "\n".join(contents)
            return te.DIRECTORY_EMPTY.format(path=directory_path)

        except Exception as e:
            logger.error(ls.DIR_LIST_ERROR.format(path=directory_path, error=e))
            return te.DIRECTORY_LIST_FAILED.format(path=directory_path)

    def _get_safe_path(self, file_path: str) -> Path:
        if Path(file_path).is_absolute():
            safe_path = Path(file_path).resolve()
        else:
            safe_path = (self.project_root / file_path).resolve()

        is_allowed = False
        for allowed_root in self.allowed_roots:
            try:
                safe_path.relative_to(allowed_root)
                is_allowed = True
                break
            except ValueError:
                continue

        if not is_allowed:
            raise PermissionError(ex.ACCESS_DENIED)

        return safe_path


def create_directory_lister_tool(directory_lister: DirectoryLister) -> Tool:
    return Tool(
        function=directory_lister.list_directory_contents,
        name=td.AgenticToolName.LIST_DIRECTORY,
        description=td.DIRECTORY_LISTER,
    )
