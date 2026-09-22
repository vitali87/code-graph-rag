"""The checked-in generated docs match what the generator produces now.

`test_generate_readme.py` tests the generator's functions; nothing compared
their output against the committed markdown (#2121). The `generate-readme`
pre-commit hook is skipped on pre-commit.ci and has no CI job behind it, so a
registry change committed without re-running the generator (80f46bbb added two
CLI commands and left the table listing 22 of 24) went out under green checks.
This runs in the required Unit Tests job instead, so drift fails the PR.
"""

from __future__ import annotations

import pytest

from codebase_rag.readme_sections import generate_all_sections
from scripts.generate_readme import (
    PROJECT_ROOT,
    SECTION_PATTERN,
    TARGET_FILES,
    replace_sections,
)

REGENERATE = "uv run python scripts/generate_readme.py"


@pytest.fixture(scope="module")
def sections() -> dict[str, str]:
    return generate_all_sections(PROJECT_ROOT)


@pytest.mark.parametrize("relative_path", TARGET_FILES)
def test_the_committed_file_is_what_the_generator_writes(
    relative_path: str, sections: dict[str, str]
) -> None:
    committed = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
    # A file with no marker the generator fills would pass the comparison
    # below vacuously, whatever its content.
    marked = {m.group(2) for m in SECTION_PATTERN.finditer(committed)}
    assert marked & sections.keys(), f"{relative_path} has no generated section"

    stale = sorted(
        name
        for name in marked & sections.keys()
        if replace_sections(committed, {name: sections[name]}) != committed
    )
    assert not stale, (
        f"{relative_path} is out of date in section(s) {stale}; run `{REGENERATE}`"
    )
