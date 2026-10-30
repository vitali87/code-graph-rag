from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from ..decorators import validate_project_path
from ..types_defs import AfterWrite
from ..utils.atomic_write import write_text_atomic
from . import tool_descriptions as td
from .schemas import FileCreationResult


class FileWriter:
    __slots__ = ("project_root", "_write_lock")

    def __init__(self, project_root: str = ".", write_lock: asyncio.Lock | None = None):
        self.project_root = Path(project_root).resolve()
        # The session's FileEditor lock when given, so a create and an edit of
        # one path from parallel tool calls run one after the other.
        self._write_lock = write_lock if write_lock is not None else asyncio.Lock()
        logger.info(ls.FILE_WRITER_INIT.format(root=self.project_root))

    async def create_file(self, file_path: str, content: str) -> FileCreationResult:
        logger.info(ls.FILE_WRITER_CREATE.format(path=file_path))
        async with self._write_lock:
            return await self._create_validated(file_path, content)

    @validate_project_path(FileCreationResult)
    async def _create_validated(
        self, file_path: Path, content: str
    ) -> FileCreationResult:
        # On a thread, as FileEditor writes: the event loop serves every
        # other request while this one waits on the disk.
        return await asyncio.to_thread(_create_file_sync, file_path, content)


def _create_file_sync(file_path: Path, content: str) -> FileCreationResult:
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        # Whole or not at all, as an edit is: a write cut short must not
        # leave a truncated file where the old one was.
        write_text_atomic(file_path, content)
        logger.info(ls.FILE_WRITER_SUCCESS.format(chars=len(content), path=file_path))
        return FileCreationResult(file_path=str(file_path))
    except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
        err_msg = te.FILE_WRITER_CREATE.format(path=file_path, error=e)
        logger.error(err_msg)
        return FileCreationResult(file_path=str(file_path), error_message=err_msg)


def create_file_writer_tool(
    file_writer: FileWriter, after_write: AfterWrite | None = None
) -> Tool:
    async def create_new_file(file_path: str, content: str) -> FileCreationResult:
        result = await file_writer.create_file(file_path, content)
        # The chat session re-ingests what it writes, so its next question
        # reads the new file (issue #2916).
        if result.success and after_write is not None:
            result.graph_update = await after_write([file_path]) or None
        return result

    return Tool(
        function=create_new_file,
        name=td.AgenticToolName.CREATE_FILE,
        description=td.FILE_WRITER,
        requires_approval=True,
    )
