#!/usr/bin/env python3
"""Prepend release news bullets to NEWS.md.

Reads a markdown fragment (the AI-generated bullets for the release being cut)
and inserts every bullet whose bold theme is not already present in NEWS.md
above the existing entries, keeping the hand-written header intact. Bullets
must match the NEWS.md entry format exactly: `- **Theme**: sentence`. Anything
else in the fragment is ignored, so a malformed or empty AI response degrades
to a no-op instead of corrupting the file. Exit code 0 always signals NEWS.md
is in a valid state; the caller decides whether a no-op warrants skipping the
follow-up README regeneration. Standard library only, so the release workflow
can run it before the project environment is synced (issue #1146).
"""

# ruff: noqa: T201
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BULLET_PATTERN = re.compile(r"^- \*\*(?P<theme>[^*]+?)\*\*: \S.*$")

MAX_ENTRIES = 3


def extract_bullets(fragment: str) -> list[str]:
    """Return the well-formed news bullets contained in a markdown fragment."""
    return [
        line.rstrip()
        for line in fragment.splitlines()
        if BULLET_PATTERN.match(line.rstrip())
    ]


def existing_themes(news: str) -> set[str]:
    """Return the casefolded bold themes of every entry already in NEWS.md."""
    return {
        match.group("theme").casefold()
        for line in news.splitlines()
        if (match := BULLET_PATTERN.match(line.strip()))
    }


def prepend_news(news: str, fragment: str) -> tuple[str, list[str]]:
    """Insert fragment bullets with unseen themes above the newest NEWS entry.

    Returns the updated NEWS.md content and the bullets that were inserted.
    Bullets whose theme already appears anywhere in NEWS.md or earlier in the
    same fragment are dropped, which makes a rerun of the same release
    idempotent, and at most MAX_ENTRIES bullets are accepted per fragment so
    an over-long AI response cannot flood the file.
    """
    themes = existing_themes(news)
    fresh: list[str] = []
    for bullet in extract_bullets(fragment):
        match = BULLET_PATTERN.match(bullet)
        if match is None:
            continue
        theme = match.group("theme").casefold()
        if theme in themes:
            continue
        fresh.append(bullet)
        themes.add(theme)
        if len(fresh) == MAX_ENTRIES:
            break
    if not fresh:
        return news, []

    lines = news.splitlines(keepends=True)
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if BULLET_PATTERN.match(line.strip()):
            insert_at = index
            break

    if not news.endswith("\n"):
        lines.append("\n")
    updated = (
        lines[:insert_at] + [f"{bullet}\n" for bullet in fresh] + lines[insert_at:]
    )
    return "".join(updated), fresh


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: update_news.py <bullets-file>", file=sys.stderr)
        return 2

    fragment_path = Path(sys.argv[1])
    if not fragment_path.is_file():
        print(f"no bullets file at {fragment_path}, nothing to do")
        return 0

    news_path = PROJECT_ROOT / "NEWS.md"
    news = news_path.read_text(encoding="utf-8")
    updated, inserted = prepend_news(news, fragment_path.read_text(encoding="utf-8"))
    if not inserted:
        print("no new themes to add, NEWS.md unchanged")
        return 0

    news_path.write_text(updated, encoding="utf-8")
    for bullet in inserted:
        print(f"added: {bullet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
