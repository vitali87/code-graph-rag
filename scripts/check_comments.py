#!/usr/bin/env python3
import sys


def find_comment_start(line: str) -> int | None:
    """Find the index where a real comment starts, or None if no comment.

    Properly handles quotes inside strings by tracking string state.
    """
    in_string = None  # (H) None, '"', or "'"
    i = 0
    while i < len(line):
        char = line[i]

        # (H) Handle escape sequences
        if char == "\\" and in_string and i + 1 < len(line):
            i += 2
            continue

        # (H) Handle string delimiters
        if char in ('"', "'"):
            if in_string is None:
                in_string = char
            elif in_string == char:
                in_string = None
            i += 1
            continue

        # (H) Hash outside of string is a comment
        if char == "#" and in_string is None:
            return i

        i += 1

    return None


def check_file(filepath: str) -> list[str]:
    errors = []

    with open(filepath) as f:
        lines = f.readlines()

    in_multiline_string = False
    found_first_code = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        # (H) Track multiline strings
        triple_quotes = line.count('"""') + line.count("'''")
        if triple_quotes % 2 == 1:
            in_multiline_string = not in_multiline_string

        if in_multiline_string:
            continue

        # (H) Track first code line
        if not found_first_code:
            if (
                stripped
                and not stripped.startswith("#")
                and not stripped.startswith('"""')
                and not stripped.startswith("'''")
            ):
                found_first_code = True

        if not found_first_code:
            continue

        # (H) Find comment using proper string parsing
        comment_idx = find_comment_start(line)
        if comment_idx is not None:
            comment_part = line[comment_idx:]
            if (
                "(H)" not in comment_part
                and "type:" not in comment_part
                and "noqa" not in comment_part
                and "pyright" not in comment_part
                and "ty:" not in comment_part
            ):
                errors.append(f"{filepath}:{i}: {comment_part.strip()[:60]}")

    return errors


def main() -> int:
    all_errors = []

    for filepath in sys.argv[1:]:
        errors = check_file(filepath)
        all_errors.extend(errors)

    if all_errors:
        print("Comments without (H) marker found:")
        for error in all_errors:
            print(f"  {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
