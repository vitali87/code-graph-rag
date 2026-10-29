from __future__ import annotations

import os
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from ..utils.path_utils import resolve_in_root
from . import tool_descriptions as td


class DirectoryLister:
    __slots__ = ("project_root",)

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def list_directory_contents(self, directory_path: str) -> str:
        target_path = resolve_in_root(self.project_root, directory_path)
        if target_path is None:
            return te.failure(
                te.PATH_OUTSIDE_ROOT.format(path=directory_path, root=self.project_root)
            )

        logger.info(ls.DIR_LISTING.format(path=target_path))

        try:
            if not target_path.is_dir():
                return te.ToolFailure(te.DIRECTORY_INVALID.format(path=directory_path))

            if contents := sorted(os.listdir(target_path)):
                return "\n".join(contents)
            return te.DIRECTORY_EMPTY.format(path=directory_path)

        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.DIR_LIST_ERROR.format(path=directory_path, error=e))
            return te.ToolFailure(te.DIRECTORY_LIST_FAILED.format(path=directory_path))


def create_directory_lister_tool(directory_lister: DirectoryLister) -> Tool:
    return Tool(
        function=directory_lister.list_directory_contents,
        name=td.AgenticToolName.LIST_DIRECTORY,
        description=td.DIRECTORY_LISTER,
    )
