from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..decorators import validate_project_path
from ..schemas import FileCreationResult
from . import tool_descriptions as td


class FileWriter:
    def __init__(self, project_root: str = ".", mode: str = "edit"):
        self.project_root = Path(project_root).resolve()
        self.mode = mode
        self.allowed_roots: frozenset[Path] | None = None
        logger.info(ls.FILE_WRITER_INIT.format(root=self.project_root))

    async def create_file(self, file_path: str, content: str) -> FileCreationResult:
        logger.info(ls.FILE_WRITER_CREATE.format(path=file_path))
        return await self._create_validated(file_path, content)

    @validate_project_path(FileCreationResult, path_arg_name="file_path")
    async def _create_validated(
        self, file_path: Path, content: str
    ) -> FileCreationResult:
        if self.mode == "query":
            logger.error(ls.QUERY_MODE_WRITE_BLOCKED.format(path=file_path))
            return FileCreationResult(
                file_path=str(file_path),
                success=False,
                error_message=te.WRITE_QUERY_MODE_BLOCKED,
            )

        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding=cs.ENCODING_UTF8)
            logger.info(
                ls.FILE_WRITER_SUCCESS.format(chars=len(content), path=file_path)
            )
            return FileCreationResult(file_path=str(file_path))
        except Exception as e:
            err_msg = te.FILE_WRITER_CREATE.format(path=file_path, error=e)
            logger.error(err_msg)
            return FileCreationResult(file_path=str(file_path), error_message=err_msg)


def create_file_writer_tool(file_writer: FileWriter) -> Tool:
    async def create_new_file(file_path: str, content: str) -> FileCreationResult:
        return await file_writer.create_file(file_path, content)

    return Tool(
        function=create_new_file,
        name=td.AgenticToolName.CREATE_FILE,
        description=td.FILE_WRITER,
        requires_approval=True,
    )
