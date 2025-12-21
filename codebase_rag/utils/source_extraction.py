from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from ..constants import (
    ENCODING_UTF8,
    LOG_SOURCE_AST_FAILED,
    LOG_SOURCE_EXTRACT_FAILED,
    LOG_SOURCE_FILE_NOT_FOUND,
    LOG_SOURCE_INVALID_RANGE,
    LOG_SOURCE_RANGE_EXCEEDS,
)


def extract_source_lines(
    file_path: Path, start_line: int, end_line: int, encoding: str = ENCODING_UTF8
) -> str | None:
    if not file_path.exists():
        logger.warning(LOG_SOURCE_FILE_NOT_FOUND.format(path=file_path))
        return None

    if start_line < 1 or end_line < 1 or start_line > end_line:
        logger.warning(LOG_SOURCE_INVALID_RANGE.format(start=start_line, end=end_line))
        return None

    try:
        with open(file_path, encoding=encoding) as f:
            lines = f.readlines()

            if start_line > len(lines) or end_line > len(lines):
                logger.warning(
                    LOG_SOURCE_RANGE_EXCEEDS.format(
                        start=start_line,
                        end=end_line,
                        length=len(lines),
                        path=file_path,
                    )
                )
                return None

            extracted_lines = lines[start_line - 1 : end_line]
            return "".join(extracted_lines).strip()

    except Exception as e:
        logger.warning(LOG_SOURCE_EXTRACT_FAILED.format(path=file_path, error=e))
        return None


def extract_source_with_fallback(
    file_path: Path,
    start_line: int,
    end_line: int,
    qualified_name: str | None = None,
    ast_extractor: Callable[[str, Path], str | None] | None = None,
    encoding: str = ENCODING_UTF8,
) -> str | None:
    if ast_extractor and qualified_name:
        try:
            if ast_result := ast_extractor(qualified_name, file_path):
                return str(ast_result)
        except Exception as e:
            logger.debug(LOG_SOURCE_AST_FAILED.format(name=qualified_name, error=e))

    return extract_source_lines(file_path, start_line, end_line, encoding)


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
