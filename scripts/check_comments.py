#!/usr/bin/env python3
import sys


def check_file(filepath: str) -> list[str]:
    errors = []

    with open(filepath) as f:
        lines = f.readlines()

    in_string = False
    found_first_code = False

    for i, line in enumerate(lines, 1):
        stripped = line.strip()

        triple_quotes = line.count('"""') + line.count("'''")
        if triple_quotes % 2 == 1:
            in_string = not in_string

        if in_string:
            continue

        if not found_first_code:
            if (
                stripped
                and not stripped.startswith("#")
                and not stripped.startswith('"""')
                and not stripped.startswith("'''")
            ):
                found_first_code = True

        hash_idx = line.find("#")
        if hash_idx != -1 and found_first_code:
            pre_hash = line[:hash_idx]
            if pre_hash.count('"') % 2 == 0 and pre_hash.count("'") % 2 == 0:
                comment_part = line[hash_idx:]
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
