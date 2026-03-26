#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent

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


def main() -> None:
    from codebase_rag.readme_sections import generate_all_sections

    readme_path = PROJECT_ROOT / "README.md"
    readme_content = readme_path.read_text(encoding="utf-8")

    sections = generate_all_sections(PROJECT_ROOT)
    updated_content = replace_sections(readme_content, sections)

    readme_path.write_text(updated_content, encoding="utf-8")
    logger.success(f"Updated {readme_path}")


if __name__ == "__main__":
    main()
