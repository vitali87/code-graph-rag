from __future__ import annotations

from scripts.update_news import (
    LATEST_RELEASE_MARKER,
    create_aggregated_bullet,
    existing_themes,
    extract_all_highlights,
    extract_bullets,
    is_feature_theme,
    prepend_news,
)

NEWS = """# Latest News

Newest first. Every entry above the latest-release marker is rendered into the
README's "Latest News" section automatically by `scripts/generate_readme.py`,
so edit entries here rather than in the README.

- **Ruby Support**: Ruby joins the graph through a new pluggable ast-grep tier.
- **Data-Flow Tracing**: New `FLOWS_TO` taint edges follow values through assignments.
"""


class TestExtractBullets:
    def test_keeps_only_wellformed_entries(self) -> None:
        fragment = (
            "## Highlights\n"
            "- **Web Search**: The agent can now search the web.\n"
            "- **Empty Body**: \n"
            "- plain bullet without a theme\n"
        )
        assert extract_bullets(fragment) == [
            "- **Web Search**: The agent can now search the web."
        ]

    def test_normalises_highlights_star_bullets(self) -> None:
        # News is derived from the release's "## Highlights" section, whose
        # bullets use "* "; they must come through in the NEWS.md "- " format.
        fragment = (
            "## Highlights\n"
            "* **Web Search**: The agent can now search the web.\n"
            "* **CI Speedups**: builds now finish faster.\n"
            "* plain star bullet without a theme\n"
        )
        assert extract_bullets(fragment) == [
            "- **Web Search**: The agent can now search the web."
        ]

    def test_empty_fragment_yields_nothing(self) -> None:
        assert extract_bullets("") == []

    def test_normalises_em_and_en_dashes(self) -> None:
        # House style forbids em/en dashes; the updater must strip them even if
        # the model ignores the prompt instruction.
        em, en = chr(0x2014), chr(0x2013)
        fragment = f"- **Web Search**: search the web {em} fast, over 3{en}5 sources.\n"
        (bullet,) = extract_bullets(fragment)
        assert em not in bullet
        assert en not in bullet
        assert bullet == "- **Web Search**: search the web - fast, over 3-5 sources."

    def test_drops_non_feature_themed_bullets(self) -> None:
        fragment = (
            "- **Release Automation**: NEWS.md refreshes on every release.\n"
            "- **CI Speedups**: builds now finish faster.\n"
            "- **Bug Fixes**: assorted crashes resolved.\n"
            "- **Web Search**: the agent can now search the web.\n"
        )
        assert extract_bullets(fragment) == [
            "- **Web Search**: the agent can now search the web."
        ]


class TestAggregateHighlights:
    def test_extracts_both_highlight_markers_without_filtering(self) -> None:
        fragment = (
            "## Highlights\n"
            "* **CI Speedups**: builds now finish faster.\n"
            "- **Bug Fixes**: assorted crashes resolved.\n"
            "* **Empty Body**: \n"
        )
        assert extract_all_highlights(fragment) == [
            ("CI Speedups", "builds now finish faster."),
            ("Bug Fixes", "assorted crashes resolved."),
        ]

    def test_combines_every_highlight_into_one_bullet(self) -> None:
        highlights = [
            ("CI Speedups", "builds now finish faster."),
            ("Bug Fixes", "assorted crashes resolved."),
            ("Documentation", "the deployment guide is clearer."),
        ]
        assert create_aggregated_bullet(highlights) == (
            "- **Release Summary**: CI Speedups: builds now finish faster; "
            "Bug Fixes: assorted crashes resolved; and Documentation: the "
            "deployment guide is clearer."
        )


class TestIsFeatureTheme:
    def test_rejects_non_feature_themes(self) -> None:
        for theme in (
            "Release Automation",
            "CI",
            "Developer Experience",
            "Tooling",
            "Refactor",
            "Refactors",
            "Bug Fix",
            "Bug-Fix",
            "Release Notes",
            "Dependency Bumps",
            "Documentation",
            "Tests",
            "Performance",
        ):
            assert not is_feature_theme(theme), theme

    def test_accepts_product_feature_themes(self) -> None:
        for theme in (
            "Ruby Support",
            "Data-Flow Tracing",
            "Structural Search & Replace",
            "Web Search",
            "Dependency Graph",
            "Pipeline Analysis",
            "Workflow Visualisation",
        ):
            assert is_feature_theme(theme), theme


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
        assert updated.endswith(
            f"- **First Feature**: The very first entry.\n{LATEST_RELEASE_MARKER}\n"
        )

    def test_duplicate_theme_within_one_fragment_inserted_once(self) -> None:
        fragment = (
            "- **Web Search**: The agent can now search the web.\n"
            "- **web search**: The same theme phrased again.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == ["- **Web Search**: The agent can now search the web."]
        assert "phrased again" not in updated

    def test_every_fresh_highlight_becomes_an_entry(self) -> None:
        # The fragment is the release's curated Highlights, so no cap applies:
        # all of it is news (a cap once silently dropped two of the five
        # v0.0.720 highlights).
        fragment = (
            "- **One**: first.\n"
            "- **Two**: second.\n"
            "- **Three**: third.\n"
            "- **Four**: fourth.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert len(inserted) == 4
        assert "- **Four**" in updated

    def test_marker_placed_after_inserted_release_block(self) -> None:
        # The README renders every entry above the marker as the latest
        # release's news, so the marker must sit directly below the block
        # this release inserted.
        fragment = (
            "- **Web Search**: The agent can now search the web.\n"
            "- **Static Binaries**: Intel macOS builds link OpenSSL statically.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert len(inserted) == 2
        lines = updated.splitlines()
        marker_at = lines.index(LATEST_RELEASE_MARKER)
        assert lines[marker_at - 1].startswith("- **Static Binaries**")
        assert lines[marker_at + 1].startswith("- **Ruby Support**")

    def test_marker_moves_to_newest_release_block(self) -> None:
        first, _ = prepend_news(NEWS, "- **Web Search**: search the web.\n")
        second, inserted = prepend_news(first, "- **Voice Input**: speak to it.\n")
        assert len(inserted) == 1
        lines = second.splitlines()
        assert lines.count(LATEST_RELEASE_MARKER) == 1
        marker_at = lines.index(LATEST_RELEASE_MARKER)
        assert lines[marker_at - 1].startswith("- **Voice Input**")
        assert lines[marker_at + 1].startswith("- **Web Search**")

    def test_no_fresh_bullets_leaves_marker_untouched(self) -> None:
        first, _ = prepend_news(NEWS, "- **Web Search**: search the web.\n")
        second, inserted = prepend_news(first, "- **web search**: same theme.\n")
        assert inserted == []
        assert second == first

    def test_marker_matches_readme_renderer(self) -> None:
        # scripts/update_news.py must stay stdlib-only, so the marker literal
        # is duplicated in codebase_rag.readme_sections; they must never drift.
        from codebase_rag.readme_sections import (
            LATEST_RELEASE_MARKER as RENDER_MARKER,
        )

        assert LATEST_RELEASE_MARKER == RENDER_MARKER

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

    def test_all_rejected_highlights_become_one_summary(self) -> None:
        fragment = (
            "* **CI Speedups**: builds now finish faster.\n"
            "* **Bug Fixes**: assorted crashes resolved.\n"
            "* **Documentation**: the deployment guide is clearer.\n"
        )
        updated, inserted = prepend_news(NEWS, fragment)
        assert inserted == [
            "- **Release Summary**: CI Speedups: builds now finish faster; "
            "Bug Fixes: assorted crashes resolved; and Documentation: the "
            "deployment guide is clearer."
        ]
        assert updated.count("- **Release Summary**:") == 1

    def test_rejected_highlight_summary_is_idempotent(self) -> None:
        fragment = (
            "* **CI Speedups**: builds now finish faster.\n"
            "* **Bug Fixes**: assorted crashes resolved.\n"
        )
        once, _ = prepend_news(NEWS, fragment)
        twice, inserted = prepend_news(once, fragment)
        assert inserted == []
        assert twice == once
