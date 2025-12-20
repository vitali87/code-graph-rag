from collections.abc import Callable
from pathlib import Path

from loguru import logger


def extract_source_lines(
    file_path: Path, start_line: int, end_line: int, encoding: str = "utf-8"
) -> str | None:
    if not file_path.exists():
        logger.warning(f"Source file not found: {file_path}")
        return None

    if start_line < 1 or end_line < 1 or start_line > end_line:
        logger.warning(f"Invalid line range: {start_line}-{end_line}")
        return None

    try:
        with open(file_path, encoding=encoding) as f:
            lines = f.readlines()

            if start_line > len(lines) or end_line > len(lines):
                logger.warning(
                    f"Line range {start_line}-{end_line} exceeds file length "
                    f"{len(lines)} in {file_path}"
                )
                return None

            extracted_lines = lines[start_line - 1 : end_line]
            return "".join(extracted_lines).strip()

    except Exception as e:
        logger.warning(f"Failed to extract source from {file_path}: {e}")
        return None


def extract_source_with_fallback(
    file_path: Path,
    start_line: int,
    end_line: int,
    qualified_name: str | None = None,
    ast_extractor: Callable | None = None,
    encoding: str = "utf-8",
) -> str | None:
    if ast_extractor and qualified_name:
        try:
            if ast_result := ast_extractor(qualified_name, file_path):
                return str(ast_result)
        except Exception as e:
            logger.debug(f"AST extraction failed for {qualified_name}: {e}")

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
