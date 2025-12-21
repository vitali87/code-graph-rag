from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import Tool

from .. import logs
from .. import tool_errors as te
from ..constants import ENCODING_UTF8
from . import tool_descriptions as td


class FileCreationResult(BaseModel):
    file_path: str
    success: bool = True
    error_message: str | None = None


class FileWriter:
    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(logs.FILE_WRITER_INIT.format(root=self.project_root))

    async def create_file(self, file_path: str, content: str) -> FileCreationResult:
        logger.info(logs.FILE_WRITER_CREATE.format(path=file_path))
        try:
            full_path = (self.project_root / file_path).resolve()

            full_path.relative_to(self.project_root)

            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding=ENCODING_UTF8)
            logger.info(
                logs.FILE_WRITER_SUCCESS.format(chars=len(content), path=file_path)
            )
            return FileCreationResult(file_path=file_path)
        except ValueError:
            err_msg = te.FILE_WRITER_SECURITY.format(path=file_path)
            logger.error(err_msg)
            return FileCreationResult(
                file_path=file_path, success=False, error_message=err_msg
            )
        except Exception as e:
            err_msg = te.FILE_WRITER_CREATE.format(path=file_path, error=e)
            logger.error(err_msg)
            return FileCreationResult(
                file_path=file_path, success=False, error_message=err_msg
            )


def create_file_writer_tool(file_writer: FileWriter) -> Tool:
    async def create_new_file(file_path: str, content: str) -> FileCreationResult:
        return await file_writer.create_file(file_path, content)

    return Tool(
        function=create_new_file,
        description=td.FILE_WRITER,
        requires_approval=True,
    )
