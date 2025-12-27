#!/usr/bin/env python3
import sys

from loguru import logger

from codebase_rag import logs
from codebase_rag.constants import (
    ALLOWED_COMMENT_MARKERS,
    COMMENT_CHAR,
    ESCAPE_CHAR,
    QUOTE_CHARS,
    TRIPLE_QUOTES,
)


def find_comment_start(line: str) -> int | None:
    in_string = None
    i = 0
    while i < len(line):
        char = line[i]

        if char == ESCAPE_CHAR and in_string and i + 1 < len(line):
            i += 2
            continue

        if char in QUOTE_CHARS:
            match in_string:
                case None:
                    in_string = char
                case _ if in_string == char:
                    in_string = None
            i += 1
            continue

        if char == COMMENT_CHAR and in_string is None:
            return i

        i += 1

    return None


def _has_allowed_marker(comment: str) -> bool:
    return any(marker in comment for marker in ALLOWED_COMMENT_MARKERS)


def check_file(filepath: str) -> list[str]:
    errors = []

    with open(filepath) as f:
        lines = f.readlines()

    in_multiline_string = False
    found_first_code = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        triple_quotes = sum(line.count(q) for q in TRIPLE_QUOTES)
        if triple_quotes % 2 == 1:
            in_multiline_string = not in_multiline_string

        if in_multiline_string:
            continue

        is_code_line = (
            stripped
            and not stripped.startswith(COMMENT_CHAR)
            and not any(stripped.startswith(q) for q in TRIPLE_QUOTES)
        )
        if is_code_line and not found_first_code:
            found_first_code = True

        if not found_first_code:
            continue

        comment_idx = find_comment_start(line)
        if comment_idx is not None:
            comment_part = line[comment_idx:]
            if not _has_allowed_marker(comment_part):
                errors.append(f"{filepath}:{i}: {comment_part.strip()[:60]}")

    return errors


def main() -> int:
    all_errors = []

    for filepath in sys.argv[1:]:
        errors = check_file(filepath)
        all_errors.extend(errors)

    if all_errors:
        logger.error(logs.COMMENTS_FOUND)
        for error in all_errors:
            logger.error(logs.COMMENT_ERROR.format(error=error))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
