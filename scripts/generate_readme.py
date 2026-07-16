#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent

TARGET_FILES = (
    "README.md",
    "docs/architecture/language-support.md",
    "docs/guide/mcp-server.md",
)

SECTION_PATTERN = re.compile(
    r"(<!-- SECTION:(\w+) -->)\n(.*?)(<!-- /SECTION:\2 -->)",
    re.DOTALL,
)


def replace_sections(readme_content: str, sections: dict[str, str]) -> str:
    def replacer(match: re.Match[str]) -> str:
        start_tag = match.group(1)
        section_name = match.group(2)
        end_tag = match.group(4)

        if section_name in sections:
            return f"{start_tag}\n{sections[section_name]}\n{end_tag}"
        return match.group(0)

    return SECTION_PATTERN.sub(replacer, readme_content)


def update_file(path: Path, sections: dict[str, str]) -> bool:
    content = path.read_text(encoding="utf-8")
    new_content = replace_sections(content, sections)
    if new_content == content:
        return False
    path.write_text(new_content, encoding="utf-8")
    return True


def main() -> None:
    from codebase_rag.readme_sections import generate_all_sections

    sections = generate_all_sections(PROJECT_ROOT)
    for relative_path in TARGET_FILES:
        target = PROJECT_ROOT / relative_path
        if update_file(target, sections):
            logger.success(f"Updated {target}")


if __name__ == "__main__":
    main()
