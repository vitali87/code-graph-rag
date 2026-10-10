from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from loguru import logger

from .. import logs as ls
from ..constants import ENCODING_UTF8, PY_EXTENSIONS
from .source_encoding import decode_python_source


def read_source_lines(file_path: Path, encoding: str = ENCODING_UTF8) -> list[str]:
    """The file's lines as the indexer read them, line endings kept."""
    raw_bytes = file_path.read_bytes()
    # The indexer parsed a Python source in the encoding it declares, and
    # the lines it recorded are lines of that text (issue #2445).
    declared = (
        decode_python_source(raw_bytes, file_path)
        if file_path.suffix in PY_EXTENSIONS
        else None
    )
    text = raw_bytes.decode(encoding) if declared is None else declared
    return text.splitlines(keepends=True)


def slice_source_lines(
    lines: Sequence[str], start_line: int, end_line: int, file_path: Path
) -> str | None:
    """Lines `start_line`..`end_line` (1-based) of an already-read file."""
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


def extract_source_lines(
    file_path: Path, start_line: int, end_line: int, encoding: str = ENCODING_UTF8
) -> str | None:
    if not file_path.exists():
        logger.warning(ls.SOURCE_FILE_NOT_FOUND.format(path=file_path))
        return None

    if start_line < 1 or end_line < 1 or start_line > end_line:
        logger.warning(ls.SOURCE_INVALID_RANGE.format(start=start_line, end=end_line))
        return None

    try:
        return slice_source_lines(
            read_source_lines(file_path, encoding), start_line, end_line, file_path
        )
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
) -> str | None:
    if ast_extractor and qualified_name:
        try:
            if ast_result := ast_extractor(qualified_name, file_path):
                return str(ast_result)
        except Exception as e:
            logger.debug(ls.SOURCE_AST_FAILED, name=qualified_name, error=e)

    return extract_source_lines(file_path, start_line, end_line, encoding)


def validate_source_location(
    file_path: str | None, start_line: int | None, end_line: int | None
) -> tuple[bool, Path | None]:
    if (
        not file_path
        or start_line is None
        or end_line is None
        or start_line < 1
        or end_line < start_line
    ):
        return False, None

    try:
        path_obj = Path(str(file_path))
        return True, path_obj
    except Exception:
        return False, None
