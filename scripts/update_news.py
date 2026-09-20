#!/usr/bin/env python3
"""Prepend release news bullets to NEWS.md.

Reads a markdown fragment and inserts every bullet whose bold theme is not
already present in NEWS.md above the existing entries, keeping the hand-written
header intact. The fragment is the release's generated "## Highlights" section:
a dedicated news generation once fed the previous NEWS entries to the model as
dedup context, and the model paraphrased those old entries into fake "news"
(v0.0.720 re-announced Ruby, structural search, and data-flow tracing), so news
is now derived from the Highlights, whose prompt carries no old entries to
anchor on. Bullets may use either `- ` or the Highlights' `* ` marker, and the colon
may sit inside or outside the bold theme (`**Theme:**` or `**Theme**:`,
both of which satisfy the generator's prompt); all are normalised to the
NEWS.md entry format `- **Theme**: sentence`. Anything else
in the fragment is ignored, so a malformed or empty AI response never corrupts
the file. Exit code 0 signals NEWS.md is in a valid state, which includes a
no-op: an empty or prose-only fragment offered nothing, so nothing is missing.
Exit code 1 is reserved for the one case that used to be silent: the fragment
carries bullet-shaped lines and none of them parsed, meaning the generator's
format and the patterns here have diverged (issue #1605). The caller decides
whether a no-op warrants skipping the follow-up README regeneration. Standard
library only, so the release workflow can run it before the project environment
is synced (issue #1146).
"""

# ruff: noqa: T201
from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# The generator is asked for "a short bold theme followed by a colon" and
# satisfies that both ways: "**Theme**: body" and "**Theme:** body". Every
# v0.0.820 highlight used the second form, so not one bullet parsed, the
# release inserted no news, and the step still reported success (issue #1605).
# Both placements are accepted and the theme group excludes a trailing colon,
# so "**X:**" and "**X**:" produce the same dedup key and still match the
# entries already in NEWS.md.
#
# The colon must sit on the FIRST bold, never merely somewhere after it. This
# preserves main's behaviour rather than adding to it: main's `\*\*:` already
# rejects a later-bold theme, and the alternation keeps that. A looser
# `:?\*\*:?` would instead let the theme group end at a LATER bold, so
# "- **Improvements** to **CI automation**: ..." would capture
# theme="Improvements" and leave "CI automation" in the body. The anchoring
# keeps the theme stable for dedup and normalisation; a bold left in the body
# is separately rejected by body_themes_are_features (issue #1608).
# The separator is ` *`, not ` +`, so a bullet with no space after the colon
# parses here exactly as it does in HIGHLIGHT_BULLET below. The two disagreed
# until #1609: this pattern rejected it while HIGHLIGHT_BULLET accepted it, so
# the same line got two answers and `prepend_news` silently demoted the bullet
# into the Release Summary aggregate, or dropped it entirely when a
# well-formed sibling kept `extract_bullets` non-empty.
#
# Unified toward the LOOSER rule after measuring both. Requiring the space in
# both places is worse than the bug it fixes: a lone no-space bullet is
# currently misfiled but survives, whereas under a strict rule `prepend_news`
# inserts nothing at all and the feature is lost. This generator's input is
# not under our control, so tolerating its whitespace is the direction that
# fails safe. Normalisation is unaffected: `text` still starts at the first
# non-space, so the emitted bullet carries exactly one space either way.
# The one separator both extractors use, so they cannot disagree about what
# follows the colon. #1609 unified the QUANTIFIER, leaving this pattern with a
# literal space and HIGHLIGHT_BULLET with `\s`; a tab then still parsed in one
# and not the other, reproducing the same defect on a different character
# (CodeRabbit, PR #1627). Sharing the fragment means a future divergence has
# to be written deliberately in one place rather than drifting.
_AFTER_COLON = r"\s*"
BULLET_PATTERN = re.compile(
    rf"^- \*\*(?P<theme>[^*]+?)(?::\*\*|\*\*:){_AFTER_COLON}(?P<text>\S.*)$"
)
HIGHLIGHT_BULLET = re.compile(
    rf"^[-*]\s+\*\*(?P<theme>[^*]+?)(?::\*\*|\*\*:){_AFTER_COLON}(?P<text>\S.*)$"
)

# Any line a human would call a bullet, however malformed. Used only to tell
# "nothing was offered" apart from "what was offered did not parse"; it must
# stay looser than BULLET_PATTERN or it would reject the broken input it is
# meant to catch.
BULLET_SHAPED = re.compile(r"^[-*]\s+\S")

# Placed directly below the block of entries the latest release inserted, so
# the README's "Latest News" can render that whole block instead of a fixed
# top-N (a fixed three once hid two of the five v0.0.720 highlights). This
# script must stay stdlib-only, so the literal is duplicated in
# codebase_rag.readme_sections; a test asserts the two never drift.
LATEST_RELEASE_MARKER = "<!-- latest-release-end -->"

# Latest News is for user-facing features, never CI, developer tooling,
# release/build automation, refactors, docs, tests, or bug fixes. Any bullet
# whose theme names that kind of work is dropped so it can never reach the
# README, independent of what the AI generator emits (issue: news must be
# features, not devx).
NON_FEATURE_THEME = re.compile(
    r"\b(?:"
    r"automation|releases?|ci|cd|devx|tooling|changelog|"
    r"refactor(?:s|ing|ed)?|chore|packaging|"
    r"lint(?:ing|ers?)?|formatting|scaffolding|maintenance|"
    r"benchmarks?|coverage|hotfix(?:es)?|bumps?|"
    r"docs?|documentation|tests?|testing|perf|performance|deprecations?"
    r")\b"
    r"|bug[- ]?fix(?:es)?|developer experience|release notes",
    re.IGNORECASE,
)


def is_feature_theme(theme: str) -> bool:
    """True when a bullet's theme names a substantial product feature.

    Rejects CI, developer tooling, release/build automation, refactors, docs,
    tests, and bug fixes so they never surface in the README's Latest News.
    """
    return NON_FEATURE_THEME.search(theme) is None


BODY_THEME = re.compile(r"\*\*(?P<theme>[^*]+?)\*\*")
# Inline code is not prose: two `**kwargs`-style tokens would otherwise pair
# up into a pseudo-bold whose "theme" is the text between them. A span opens
# with a run of backticks and closes with a run of the SAME length, so
# ``**CI**`` (a double-backtick span) is one span, not two empty ones with a
# bold left between them (#1748 review).
CODE_SPAN = re.compile(r"(?P<delimiter>`+).*?(?P=delimiter)")


def body_themes_are_features(text: str) -> bool:
    """True when every bold span in a bullet's BODY passes `is_feature_theme`.

    The filter used to read the theme and nothing else, so a generator that
    wrote "**Improvements**: to **CI automation**: ..." put its real subject in
    a second bold the filter never saw, and CI content reached the README
    (issue #1608). A bold in the body is a theme in all but position, so it is
    held to the theme's rule. Plain prose is NOT: a feature entry legitimately
    says "the test suite", "type-test patterns", "this release" or "a benchmark
    harness" in passing (four current NEWS.md entries do), and rejecting on
    those words would drop the features this filter exists to keep. Code spans
    are stripped first, so `**kwargs` in backticks cannot open a bold.
    """
    prose = CODE_SPAN.sub("", text)
    return all(
        is_feature_theme(match.group("theme")) for match in BODY_THEME.finditer(prose)
    )


def _normalize_dashes(text: str) -> str:
    """Replace em-dashes and en-dashes with a hyphen; house style forbids them.

    The generator prompts already ask the model to avoid them, but a prompt is
    not a guarantee, so every bullet written into NEWS.md is normalised here.
    ``chr`` keeps the dash characters out of this source file too.
    """
    return text.replace(chr(0x2014), "-").replace(chr(0x2013), "-")


def extract_bullets(fragment: str) -> list[str]:
    """Return the well-formed, feature-themed news bullets in a fragment.

    A bullet must match the NEWS.md entry format AND name a product feature,
    in its theme and in any bold the body carries (issue #1608); non-feature
    themes (CI/devx/release/bug/etc.) are discarded here so neither
    the count cap nor the dedup downstream ever considers them. Highlights-style
    `* ` bullets are accepted and normalised to the NEWS.md `- ` marker, as
    is a colon inside the bold theme (`**Theme:**`), which the generator
    emits and which parsed to nothing before issue #1605.
    """
    bullets: list[str] = []
    for line in fragment.splitlines():
        stripped = _normalize_dashes(line.rstrip())
        if stripped.startswith("* "):
            stripped = f"- {stripped[2:]}"
        match = BULLET_PATTERN.match(stripped)
        if (
            match
            and is_feature_theme(match.group("theme"))
            and body_themes_are_features(match.group("text"))
        ):
            theme = match.group("theme").strip()
            bullets.append(f"- **{theme}**: {match.group('text')}")
    return bullets


def extract_all_highlights(fragment: str) -> list[tuple[str, str]]:
    """Extract every well-formed highlight, regardless of feature filtering.

    Used as fallback source for aggregation when no feature themes pass the filter.
    Returns list of (theme, text) tuples.
    """
    highlights: list[tuple[str, str]] = []
    for line in fragment.splitlines():
        stripped = _normalize_dashes(line.rstrip())
        match = HIGHLIGHT_BULLET.match(stripped)
        if match:
            highlights.append((match.group("theme"), match.group("text")))
    return highlights


def has_unparsable_bullets(fragment: str) -> bool:
    """True when the fragment offers bullets but none of them parse.

    Distinguishes the two ways a release inserts no news. An empty or
    prose-only highlights section is a legitimate no-op: nothing was offered,
    so nothing is missing. A section full of bullets that yields no entries
    means the generator's format and this module's patterns have diverged,
    which is a failure that must be loud - it is the exact shape of the
    v0.0.820 outage, where every bullet used ``**Theme:**`` and the parser
    accepted only ``**Theme**:``.

    Deliberately looser than BULLET_PATTERN: it asks whether a line was MEANT
    to be a bullet, so a shape the real patterns reject still counts as one.
    A stricter test here would return False on precisely the malformed input
    this exists to detect.

    Parse success is measured with extract_all_highlights, never with
    extract_bullets. extract_bullets both parses AND drops non-feature themes,
    so it returns [] for two unrelated reasons, and using it here reported a
    parse failure for an all-CI release whose bullets parsed perfectly. That
    misfires only on a RERUN: the first run inserts the aggregated summary, so
    the guard is never reached.
    """
    return any(
        BULLET_SHAPED.match(line.strip()) for line in fragment.splitlines()
    ) and not extract_all_highlights(fragment)


def existing_themes(news: str) -> set[str]:
    """Return the casefolded bold themes of every entry already in NEWS.md."""
    return {
        match.group("theme").casefold()
        for line in news.splitlines()
        if (match := BULLET_PATTERN.match(line.strip()))
    }


def create_aggregated_bullet(highlights: list[tuple[str, str]]) -> str:
    """Combine all release highlights into one fallback news bullet.

    This preserves the substance of every highlight instead of reducing the
    release to a list of theme names. It is only used when every individual
    highlight is rejected by the feature-theme filter.
    """
    summaries = [f"{theme}: {text.rstrip('.')}" for theme, text in highlights]
    if not summaries:
        return ""

    if len(summaries) == 1:
        summary = summaries[0]
    else:
        summary = f"{'; '.join(summaries[:-1])}; and {summaries[-1]}"

    return f"- **Release Summary**: {summary}."


def prepend_news(news: str, fragment: str) -> tuple[str, list[str]]:
    """Insert fragment bullets with unseen themes above the newest NEWS entry.

    Returns the updated NEWS.md content and the bullets that were inserted.
    Bullets whose theme already appears anywhere in NEWS.md or earlier in the
    same fragment are dropped, which makes a rerun of the same release
    idempotent. Every remaining Highlights bullet is accepted: the fragment is
    the release's curated Highlights section, so all of it is news. The
    latest-release marker is moved below the inserted block so the README can
    render the whole block; when nothing is inserted the marker stays put.

    If no feature-themed bullets are found, an aggregated bullet is created
    from all highlights to ensure at least one news entry per release.
    """
    themes = existing_themes(news)
    fresh: list[str] = []
    feature_bullets = extract_bullets(fragment)
    for bullet in feature_bullets:
        match = BULLET_PATTERN.match(bullet)
        if match is None:
            continue
        theme = match.group("theme").casefold()
        if theme in themes:
            continue
        fresh.append(bullet)
        themes.add(theme)

    # Preserve at least one substantive entry when every highlight is filtered.
    # Compare the whole fallback bullet rather than its generic theme so a
    # workflow rerun is idempotent while a later release can add its own summary.
    if not feature_bullets:
        all_highlights = extract_all_highlights(fragment)
        aggregated = create_aggregated_bullet(all_highlights)
        if aggregated and aggregated not in news.splitlines():
            fresh.append(aggregated)

    if not fresh:
        return news, []

    lines = [
        line
        for line in news.splitlines(keepends=True)
        if line.strip() != LATEST_RELEASE_MARKER
    ]
    insert_at = len(lines)
    for index, line in enumerate(lines):
        if BULLET_PATTERN.match(line.strip()):
            insert_at = index
            break

    if not news.endswith("\n"):
        lines.append("\n")
    block = [f"{bullet}\n" for bullet in fresh] + [f"{LATEST_RELEASE_MARKER}\n"]
    updated = lines[:insert_at] + block + lines[insert_at:]
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
    fragment = fragment_path.read_text(encoding="utf-8")
    updated, inserted = prepend_news(news, fragment)

    # Reported separately because they fail for different reasons and only the
    # first distinguishes a broken parser from a quiet release: parsed counts
    # what the patterns understood, deduped what was already in NEWS.md, and
    # inserted what actually reached the file. Collapsing them into "nothing
    # added" is what hid issue #1605 for roughly fifty releases.
    feature_bullets = extract_bullets(fragment)
    inserted_count = len(inserted)
    # When every theme is filtered out, prepend_news falls back to a single
    # aggregated "Release Summary" (added by #1600) that extract_bullets never
    # produced, so inserted can exceed parsed. Counting that as a negative
    # dedup would be nonsense; report the aggregate for what it is instead.
    aggregated = inserted_count and not feature_bullets
    parsed = len(feature_bullets)
    deduped = 0 if aggregated else parsed - inserted_count
    summary = f"parsed {parsed}, deduped {deduped}, inserted {inserted_count}"
    print(f"{summary} (aggregated fallback)" if aggregated else summary)

    if not inserted:
        if has_unparsable_bullets(fragment):
            print(
                f"{fragment_path} contains bullet-shaped lines but none parsed; "
                "the highlights format and HIGHLIGHT_BULLET have diverged",
                file=sys.stderr,
            )
            return 1
        print("no new themes to add, NEWS.md unchanged")
        return 0

    news_path.write_text(updated, encoding="utf-8")
    for bullet in inserted:
        print(f"added: {bullet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
