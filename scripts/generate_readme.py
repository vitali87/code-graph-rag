#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

PROJECT_ROOT = Path(__file__).parent.parent

TARGET_FILES = (
    "README.md",
    "docs/architecture/language-support.md",
    "docs/architecture/graph-schema.md",
    "docs/guide/mcp-server.md",
    "docs/guide/interactive-querying.md",
    "docs/guide/cli-reference.md",
    "docs/getting-started/installation.md",
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


def marked_sections(content: str) -> set[str]:
    """The section names `content` carries a marker pair for."""
    return {match.group(2) for match in SECTION_PATTERN.finditer(content)}


def unconsumed_sections(sections: dict[str, str], targets: list[Path]) -> list[str]:
    """Generated sections no target file carries a marker for.

    The substitution is a no-op when no marker matches, and the hook only
    reports a file whose digest changed, so a section without a marker was
    computed on every run and silently discarded: the schema tables in
    `graph-schema.md` were hand-maintained for that reason, and three node
    labels were undocumented before anyone noticed (issue #1929).
    """
    marked: set[str] = set()
    for target in targets:
        marked |= marked_sections(target.read_text(encoding="utf-8"))
    return sorted(set(sections) - marked)


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
    targets = [PROJECT_ROOT / relative_path for relative_path in TARGET_FILES]
    for target in targets:
        if update_file(target, sections):
            logger.success(f"Updated {target}")
    # After the substitution, not before: a section is consumed by a marker
    # somewhere in the target files, and an unconsumed one is an error, not a
    # silent no-op. Each name below needs a `<!-- SECTION:<name> -->` pair.
    if missing := unconsumed_sections(sections, targets):
        logger.error(
            "Generated section(s) reach no marker in any target file: "
            f"{', '.join(missing)}. Add `<!-- SECTION:<name> -->` / "
            "`<!-- /SECTION:<name> -->` markers where each table belongs."
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
