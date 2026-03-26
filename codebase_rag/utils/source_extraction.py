from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .. import logs as ls
from ..config import settings
from ..constants import BYTES_PER_MB, ENCODING_UTF8


class SourceFileCache:
    __slots__ = ("_cache", "_current_bytes", "_max_entries", "_max_memory_bytes")

    def __init__(
        self, max_entries: int | None = None, max_memory_mb: int | None = None
    ) -> None:
        self._cache: OrderedDict[Path, tuple[list[str], int]] = OrderedDict()
        self._current_bytes = 0
        self._max_entries = (
            max_entries if max_entries is not None else settings.SOURCE_CACHE_MAX_ENTRIES
        )
        max_mem = (
            max_memory_mb
            if max_memory_mb is not None
            else settings.SOURCE_CACHE_MAX_MEMORY_MB
        )
        self._max_memory_bytes = max_mem * BYTES_PER_MB

    def get(self, file_path: Path, encoding: str) -> list[str]:
        if file_path in self._cache:
            lines, size_bytes = self._cache[file_path]
            self._cache.move_to_end(file_path)
            return lines

        raw_bytes = file_path.read_bytes()
        text = raw_bytes.decode(encoding)
        lines = text.splitlines(keepends=True)
        size_bytes = len(raw_bytes)

        self._cache[file_path] = (lines, size_bytes)
        self._current_bytes += size_bytes
        self._enforce_limits()

        return lines

    def _enforce_limits(self) -> None:
        while len(self._cache) > self._max_entries:
            _, (_, size_bytes) = self._cache.popitem(last=False)
            self._current_bytes -= size_bytes

        while self._current_bytes > self._max_memory_bytes and self._cache:
            _, (_, size_bytes) = self._cache.popitem(last=False)
            self._current_bytes -= size_bytes


def extract_source_lines(
    file_path: Path,
    start_line: int,
    end_line: int,
    encoding: str = ENCODING_UTF8,
    cache: SourceFileCache | None = None,
) -> str | None:
    if not file_path.exists():
        logger.warning(ls.SOURCE_FILE_NOT_FOUND.format(path=file_path))
        return None

    if start_line < 1 or end_line < 1 or start_line > end_line:
        logger.warning(ls.SOURCE_INVALID_RANGE.format(start=start_line, end=end_line))
        return None

    try:
        if cache is not None:
            lines = cache.get(file_path, encoding)
        else:
            raw_bytes = file_path.read_bytes()
            text = raw_bytes.decode(encoding)
            lines = text.splitlines(keepends=True)

        if not lines:
            return None

        if start_line > len(lines) or end_line > len(lines):
            logger.warning(
                ls.SOURCE_RANGE_EXCEEDS.format(
                    start=start_line,
                    end=end_line,
                    length=len(lines),
                    path=file_path,
                )
            )
            end_line = min(end_line, len(lines))
            if start_line > len(lines):
                return None

        extracted_lines = lines[start_line - 1 : end_line]
        return "".join(extracted_lines).strip()

    except Exception as e:
        logger.warning(ls.SOURCE_EXTRACT_FAILED.format(path=file_path, error=e))
        return None


def extract_source_with_fallback(
    file_path: Path,
    start_line: int,
    end_line: int,
    qualified_name: str | None = None,
    ast_extractor: Callable[[str, Path], str | None] | None = None,
    encoding: str = ENCODING_UTF8,
    cache: SourceFileCache | None = None,
) -> str | None:
    if ast_extractor and qualified_name:
        try:
            if ast_result := ast_extractor(qualified_name, file_path):
                return str(ast_result)
        except Exception as e:
            logger.debug(ls.SOURCE_AST_FAILED, name=qualified_name, error=e)

    return extract_source_lines(file_path, start_line, end_line, encoding, cache=cache)


def validate_source_location(
    file_path: str | None, start_line: int | None, end_line: int | None
) -> tuple[bool, Path | None]:
    if not all([file_path, start_line, end_line]):
        return False, None

    try:
        path_obj = Path(str(file_path))
        return True, path_obj
    except Exception:
        return False, None
