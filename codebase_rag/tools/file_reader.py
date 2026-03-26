from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..config import settings
from ..decorators import validate_project_path
from ..schemas import FileReadResult
from ..utils.cache import EvictingCache
from . import tool_descriptions as td


class FileReader:
    __slots__ = ("project_root", "_cache")

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()

        def calculate_size(content: str) -> int:
            return len(content.encode(cs.ENCODING_UTF8))

        self._cache = EvictingCache[str, str](
            max_entries=settings.SOURCE_CACHE_MAX_ENTRIES,
            max_size=settings.SOURCE_CACHE_MAX_MEMORY_MB * 1024 * 1024,
            size_func=calculate_size,
        )
        logger.info(ls.FILE_READER_INIT.format(root=self.project_root))

    async def read_file(self, file_path: str) -> FileReadResult:
        logger.info(ls.TOOL_FILE_READ.format(path=file_path))
        return await self._read_validated(file_path)

    @validate_project_path(FileReadResult, path_arg_name="file_path")
    async def _read_validated(self, file_path: Path) -> FileReadResult:
        try:
            cache_key = str(file_path)
            if cached_content := self._cache.get(cache_key):
                logger.info(ls.TOOL_FILE_READ_SUCCESS.format(path=file_path))
                return FileReadResult(file_path=cache_key, content=cached_content)

            if not file_path.is_file():
                return FileReadResult(
                    file_path=str(file_path), error_message=te.FILE_NOT_FOUND
                )

            if file_path.suffix.lower() in cs.BINARY_EXTENSIONS:
                error_msg = te.BINARY_FILE.format(path=file_path)
                logger.warning(ls.TOOL_FILE_BINARY.format(message=error_msg))
                return FileReadResult(file_path=str(file_path), error_message=error_msg)

            try:
                content = file_path.read_text(encoding=cs.ENCODING_UTF8)
                if len(content) > settings.MAX_FILE_READ_CHARS:
                    content = (
                        content[: settings.MAX_FILE_READ_CHARS]
                        + "\n\n[Truncated file output]"
                    )
                self._cache.put(cache_key, content)
                logger.info(ls.TOOL_FILE_READ_SUCCESS.format(path=file_path))
                return FileReadResult(file_path=str(file_path), content=content)
            except UnicodeDecodeError:
                error_msg = te.UNICODE_DECODE.format(path=file_path)
                logger.warning(ls.TOOL_FILE_BINARY.format(message=error_msg))
                return FileReadResult(file_path=str(file_path), error_message=error_msg)

        except Exception as e:
            logger.error(ls.FILE_READER_ERR.format(path=file_path, error=e))
            return FileReadResult(
                file_path=str(file_path),
                error_message=ls.UNEXPECTED.format(error=e),
            )


def create_file_reader_tool(file_reader: FileReader) -> Tool:
    async def read_file_content(file_path: str) -> str:
        result = await file_reader.read_file(file_path)
        if result.error_message:
            return te.ERROR_WRAPPER.format(message=result.error_message)
        return result.content or ""

    return Tool(
        function=read_file_content,
        name=td.AgenticToolName.READ_FILE,
        description=td.FILE_READER,
    )
