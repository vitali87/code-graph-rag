from __future__ import annotations

from pathlib import Path

from loguru import logger
from pydantic_ai import ApprovalRequired, RunContext, Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..decorators import validate_project_path
from ..schemas import FileReadResult
from ..sensitive_paths import is_sensitive_path
from ..taint import ReadContentRecord
from ..utils.source_encoding import source_codec
from . import tool_descriptions as td


class FileReader:
    __slots__ = ("project_root",)

    def __init__(self, project_root: str = "."):
        self.project_root = Path(project_root).resolve()
        logger.info(ls.FILE_READER_INIT.format(root=self.project_root))

    async def read_file(self, file_path: str) -> FileReadResult:
        logger.info(ls.TOOL_FILE_READ.format(path=file_path))
        return await self._read_validated(file_path)

    def is_sensitive(self, file_path: str) -> bool:
        """Whether `file_path`, taken from the project root, looks like a
        credential store (issue #3457)."""
        return is_sensitive_path(self.project_root / file_path)

    @validate_project_path(FileReadResult, path_arg_name="file_path")
    async def _read_validated(self, file_path: Path) -> FileReadResult:
        """Read a path already checked to be inside the project root."""
        try:
            if not file_path.is_file():
                return FileReadResult(
                    file_path=str(file_path), error_message=te.FILE_NOT_FOUND
                )

            if file_path.suffix.lower() in cs.BINARY_EXTENSIONS:
                error_msg = te.BINARY_FILE.format(path=file_path)
                logger.warning(ls.TOOL_FILE_BINARY.format(message=error_msg))
                return FileReadResult(file_path=str(file_path), error_message=error_msg)

            try:
                # In the encoding a Python file declares, as the indexer
                # read it (issue #2901).
                content = file_path.read_text(encoding=source_codec(file_path))
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


async def read_file_text(
    file_reader: FileReader,
    file_path: str,
    read_record: ReadContentRecord | None = None,
) -> str:
    """Read a file as tool output, recording its content on success."""
    result = await file_reader.read_file(file_path)
    if result.error_message:
        return te.failure(result.error_message)
    content = result.content or ""
    if read_record is not None:
        # Feed the egress taint gate (issue #1128): this content must
        # never later appear verbatim in an outbound web query.
        read_record.record(content)
    return content


def create_file_reader_tool(
    file_reader: FileReader, read_record: ReadContentRecord | None = None
) -> Tool[None]:
    """Build the `read_file` tool, recording file content in `read_record` so
    it feeds the egress taint gate (issue #1128)."""

    async def read_file_content(ctx: RunContext[None], file_path: str) -> str:
        """Read a file; a likely credential store waits for the operator."""
        # Deferred rather than refused, so the operator can still let one
        # through; a run with nobody to ask (`cgr ask`, MCP ask_agent)
        # denies it (issue #3457).
        if not ctx.tool_call_approved and file_reader.is_sensitive(file_path):
            raise ApprovalRequired(metadata={cs.ARG_FILE_PATH: file_path})
        return await read_file_text(file_reader, file_path, read_record)

    return Tool[None](
        function=read_file_content,
        name=td.AgenticToolName.READ_FILE,
        description=td.FILE_READER,
    )
