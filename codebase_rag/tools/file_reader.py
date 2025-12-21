from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic import BaseModel
from pydantic_ai import Tool

from .. import logs as ls
from .. import tool_errors as te
from ..constants import ENCODING_UTF8
from . import tool_descriptions as td


class FileReadResult(BaseModel):
    file_path: str
    content: str | None = None
    error_message: str | None = None


class FileReader:
    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        self.binary_extensions = {
            ".pdf",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".ico",
            ".tiff",
            ".webp",
        }
        logger.info(ls.FILE_READER_INIT.format(root=self.project_root))

    async def read_file(self, file_path: str) -> FileReadResult:
        logger.info(ls.TOOL_FILE_READ.format(path=file_path))
        try:
            full_path = (self.project_root / file_path).resolve()
            try:
                full_path.relative_to(self.project_root.resolve())
            except ValueError:
                return FileReadResult(
                    file_path=file_path,
                    error_message=ls.FILE_OUTSIDE_ROOT.format(action="read"),
                )

            if not str(full_path).startswith(str(self.project_root.resolve())):
                return FileReadResult(
                    file_path=file_path,
                    error_message=ls.FILE_OUTSIDE_ROOT.format(action="read"),
                )

            if not full_path.is_file():
                return FileReadResult(
                    file_path=file_path, error_message=te.FILE_NOT_FOUND
                )

            if full_path.suffix.lower() in self.binary_extensions:
                error_msg = te.BINARY_FILE.format(path=file_path)
                logger.warning(ls.TOOL_FILE_BINARY.format(message=error_msg))
                return FileReadResult(file_path=file_path, error_message=error_msg)

            try:
                content = full_path.read_text(encoding=ENCODING_UTF8)
                logger.info(ls.TOOL_FILE_READ_SUCCESS.format(path=file_path))
                return FileReadResult(file_path=file_path, content=content)
            except UnicodeDecodeError:
                error_msg = te.UNICODE_DECODE.format(path=file_path)
                logger.warning(ls.TOOL_FILE_BINARY.format(message=error_msg))
                return FileReadResult(file_path=file_path, error_message=error_msg)

        except ValueError:
            return FileReadResult(
                file_path=file_path,
                error_message=ls.FILE_OUTSIDE_ROOT.format(action="read"),
            )
        except Exception as e:
            logger.error(ls.FILE_READER_ERR.format(path=file_path, error=e))
            return FileReadResult(
                file_path=file_path,
                error_message=ls.UNEXPECTED.format(error=e),
            )


def create_file_reader_tool(file_reader: FileReader) -> Tool:
    async def read_file_content(file_path: str) -> str:
        result = await file_reader.read_file(file_path)
        if result.error_message:
            return f"Error: {result.error_message}"
        return result.content or ""

    return Tool(
        function=read_file_content,
        description=td.FILE_READER,
    )
