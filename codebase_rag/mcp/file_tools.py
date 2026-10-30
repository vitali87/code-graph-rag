"""The MCP tools that act on this server's files: read, write, list,
surgical and structural replacement and structural search. A write appends
the structural delta of re-ingesting what it changed (issue #1525).
"""

import itertools
import sys
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag import tool_errors as te
from codebase_rag.tools.file_reader import read_refusal
from codebase_rag.types_defs import StructuralReplaceChange
from codebase_rag.utils.path_utils import resolve_in_root
from codebase_rag.utils.source_encoding import source_codec

from . import constants as mcs
from .handler_base import plain_function, run_in_thread
from .lifecycle_tools import LifecycleTools


def _read_file_slice(
    full_path: Path, start: int, limit: int | None, file_path: str
) -> str:
    # The checks and the decoding a whole-file read applies (FileReader).
    if refusal := read_refusal(full_path):
        return te.failure(refusal)
    try:
        return _paginate(full_path, start, limit, file_path)
    except UnicodeDecodeError:
        return te.failure(te.UNICODE_DECODE.format(path=full_path))


def _paginate(full_path: Path, start: int, limit: int | None, file_path: str) -> str:
    with open(full_path, encoding=source_codec(full_path)) as f:
        # islice() refuses a count past sys.maxsize; no file has that many
        # lines, so the clamped skip reads to the same end.
        skipped_count = sum(1 for _ in itertools.islice(f, min(start, sys.maxsize)))

        if limit is not None:
            sliced_lines = [line for _, line in zip(range(limit), f)]
        else:
            sliced_lines = list(f)

        paginated_content = "".join(sliced_lines)
        remaining_lines_count = sum(1 for _ in f)

    total_lines = skipped_count + len(sliced_lines) + remaining_lines_count
    # Offset 0 is the start even of an empty file; any other offset must
    # land on a line.
    if start >= total_lines and start > 0:
        return te.failure(
            mcs.MCP_READ_PAST_END.format(
                offset=start, path=file_path, total=total_lines
            )
        )
    header = mcs.MCP_PAGINATION_HEADER.format(
        start=start + 1,
        end=start + len(sliced_lines),
        total=total_lines,
    )
    return header + paginated_content


class FileTools(LifecycleTools):
    """Reads and writes of the files under this server's root."""

    async def structural_search(self, pattern: str, language: str | None = None) -> str:
        result = await plain_function(self._structural_search_tool)(
            pattern=pattern, language=language
        )
        if isinstance(result, te.ToolFailure):
            return result
        return str(result)

    async def structural_replace(
        self,
        pattern: str,
        rewrite: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> str:
        # The write and the re-ingest that follows it share the lock, so a
        # rebuild cannot land between the two (issue #1525).
        async with self._ingestor_lock:
            self._structural_written = []
            result = await plain_function(self._structural_editor_tool)(
                pattern=pattern, rewrite=rewrite, language=language, dry_run=dry_run
            )
            written = list(self._structural_written)
            if isinstance(result, te.ToolFailure):
                return result
            if dry_run or not written:
                return str(result)
            return str(result) + await run_in_thread(self._delta_after_write, written)

    def _note_structural_changes(self, changes: list[StructuralReplaceChange]) -> None:
        self._structural_written = [c["file"] for c in changes if c["applied"]]

    async def surgical_replace_code(
        self, file_path: str, target_code: str, replacement_code: str
    ) -> str:
        logger.info(ls.MCP_SURGICAL_REPLACE.format(path=file_path))
        try:
            async with self._ingestor_lock:
                result = await plain_function(self._file_editor_tool)(
                    file_path=file_path,
                    target_code=target_code,
                    replacement_code=replacement_code,
                )
                if isinstance(result, te.ToolFailure):
                    return result
                delta = await run_in_thread(self._delta_after_write, [file_path])
                return str(result) + delta
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_REPLACE.format(error=e))
            return te.failure(e)

    async def read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        logger.info(ls.MCP_READ_FILE.format(path=file_path, offset=offset, limit=limit))
        if offset is not None and offset < mcs.MCP_READ_OFFSET_MINIMUM:
            return te.failure(mcs.MCP_READ_OFFSET_NEGATIVE.format(offset=offset))
        if limit is not None and limit < mcs.MCP_READ_LIMIT_MINIMUM:
            return te.failure(mcs.MCP_READ_LIMIT_NOT_POSITIVE.format(limit=limit))
        try:
            if offset is not None or limit is not None:
                project_root = Path(self.project_root)
                full_path = resolve_in_root(project_root, file_path)
                if full_path is None:
                    return te.failure(
                        te.PATH_OUTSIDE_ROOT.format(
                            path=file_path, root=project_root.resolve()
                        )
                    )
                start = offset if offset is not None else 0
                return await run_in_thread(
                    _read_file_slice, full_path, start, limit, file_path
                )
            else:
                # Returned as is: a ToolFailure must stay one (issue #2650).
                return await plain_function(self._file_reader_tool)(file_path=file_path)

        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_READ.format(error=e))
            return te.failure(e)

    async def write_file(self, file_path: str, content: str) -> str:
        logger.info(ls.MCP_WRITE_FILE.format(path=file_path))
        try:
            async with self._ingestor_lock:
                result = await plain_function(self._file_writer_tool)(
                    file_path=file_path, content=content
                )
                if not result.success:
                    return te.failure(result.error_message)
                delta = await run_in_thread(self._delta_after_write, [file_path])
                return mcs.MCP_WRITE_SUCCESS.format(path=file_path) + delta
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_WRITE.format(error=e))
            return te.failure(e)

    async def list_directory(
        self, directory_path: str = cs.MCP_DEFAULT_DIRECTORY
    ) -> str:
        logger.info(ls.MCP_LIST_DIR.format(path=directory_path))
        try:
            return await run_in_thread(
                plain_function(self._directory_lister_tool),
                directory_path=directory_path,
            )
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_LIST_DIR.format(error=e))
            return te.failure(e)
