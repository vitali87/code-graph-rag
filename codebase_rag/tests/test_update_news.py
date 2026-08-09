from __future__ import annotations

from scripts.update_news import existing_themes, extract_bullets, prepend_news

NEWS = """# Latest News

Newest first. The top three entries are rendered into the README's "Latest News"
section automatically by `scripts/generate_readme.py`, so edit them here rather
than in the README.

- **Ruby Support**: Ruby joins the graph through a new pluggable ast-grep tier.
- **Data-Flow Tracing**: New `FLOWS_TO` taint edges follow values through assignments.
"""


class TestExtractBullets:
    def test_keeps_only_wellformed_entries(self) -> None:
        fragment = (
            "## Highlights\n"
            "- **Web Search**: The agent can now search the web.\n"
            "* **Wrong Marker**: star bullets are not the NEWS format.\n"
            "- **Empty Body**: \n"
            "- plain bullet without a theme\n"
        )
        assert extract_bullets(fragment) == [
            "- **Web Search**: The agent can now search the web."
        ]

    def test_empty_fragment_yields_nothing(self) -> None:
        assert extract_bullets("") == []


class TestExistingThemes:
    def test_collects_casefolded_themes(self) -> None:
        assert existing_themes(NEWS) == {"ruby support", "data-flow tracing"}


class TestPrependNews:
    def test_inserts_above_newest_entry_preserving_header(self) -> None:
        fragment = "- **Web Search**: The agent can now search the web.\n"
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == ["- **Web Search**: The agent can now search the web."]
        bullet_lines = [
            line for line in updated.splitlines() if line.startswith("- **")
        ]
        assert bullet_lines[0].startswith("- **Web Search**")
        assert bullet_lines[1].startswith("- **Ruby Support**")
        assert updated.startswith("# Latest News")

    def test_duplicate_theme_is_dropped_case_insensitively(self) -> None:
        fragment = "- **ruby support**: Ruby again, phrased differently.\n"
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == []
        assert updated == NEWS

    def test_rerun_with_same_fragment_is_idempotent(self) -> None:
        fragment = "- **Web Search**: The agent can now search the web.\n"
        once, _ = prepend_news(NEWS, fragment)
        twice, inserted = prepend_news(once, fragment)
        assert inserted == []
        assert twice == once

    def test_news_without_entries_appends_after_header(self) -> None:
        header_only = "# Latest News\n\nNothing yet.\n"
        fragment = "- **First Feature**: The very first entry.\n"
        updated, inserted = prepend_news(header_only, fragment)
        assert inserted == ["- **First Feature**: The very first entry."]
        assert updated.endswith("- **First Feature**: The very first entry.\n")

    def test_duplicate_theme_within_one_fragment_inserted_once(self) -> None:
        fragment = (
            "- **Web Search**: The agent can now search the web.\n"
            "- **web search**: The same theme phrased again.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == ["- **Web Search**: The agent can now search the web."]
        assert "phrased again" not in updated

    def test_fragment_is_capped_at_three_entries(self) -> None:
        fragment = (
            "- **One**: first.\n"
            "- **Two**: second.\n"
            "- **Three**: third.\n"
            "- **Four**: fourth.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert len(inserted) == 3
        assert "- **Four**" not in updated

    def test_mixed_fragment_inserts_only_fresh_themes(self) -> None:
        fragment = (
            "- **Web Search**: The agent can now search the web.\n"
            "- **Data-Flow Tracing**: already covered by an older entry.\n"
            "- **Static Binaries**: Intel macOS builds link OpenSSL statically.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == [
            "- **Web Search**: The agent can now search the web.",
            "- **Static Binaries**: Intel macOS builds link OpenSSL statically.",
        ]
        assert "already covered" not in updated
