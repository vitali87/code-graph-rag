from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import update_news
from scripts.update_news import (
    BULLET_PATTERN,
    HIGHLIGHT_BULLET,
    LATEST_RELEASE_MARKER,
    body_themes_are_features,
    create_aggregated_bullet,
    existing_themes,
    extract_all_highlights,
    extract_bullets,
    has_unparsable_bullets,
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

    def test_accepts_a_colon_inside_the_bold_theme(self) -> None:
        # The generator is asked for "a short bold theme followed by a colon",
        # which it satisfies both ways: "**Theme**:" and "**Theme:**". Every
        # v0.0.820 highlight used the second form, so no bullet parsed, the
        # release inserted no news, and the run still reported success. Accept
        # both placements rather than relying on the model's choice.
        fragment = (
            "## Highlights\n"
            "* **Parsing Improvements:** PHP `use function` imports resolve.\n"
            "* **Graph Enhancements:**  Call-site locations are stored on edges.\n"
        )
        assert extract_bullets(fragment) == [
            "- **Parsing Improvements**: PHP `use function` imports resolve.",
            "- **Graph Enhancements**: Call-site locations are stored on edges.",
        ]

    def test_both_patterns_accept_both_colon_placements(self) -> None:
        # main carries TWO patterns: BULLET_PATTERN (NEWS "- " entries) and
        # HIGHLIGHT_BULLET (release "* " highlights). Both required the colon
        # OUTSIDE the bold while the generator emits it inside, so both need
        # the alternation or the aggregation fallback stays blind (#1605).
        for line in (
            "- **Parsing Improvements:** body.",
            "- **Parsing Improvements**: body.",
        ):
            match = BULLET_PATTERN.match(line)
            assert match is not None, line
            assert match.group("theme") == "Parsing Improvements", match.group("theme")
        for line in (
            "* **Parsing Improvements:** body.",
            "* **Parsing Improvements**: body.",
        ):
            match = HIGHLIGHT_BULLET.match(line)
            assert match is not None, line
            assert match.group("theme") == "Parsing Improvements", match.group("theme")

    def test_extract_bullets_consumes_the_named_group_end_to_end(self) -> None:
        # The regex group feeding the f-string in extract_bullets is named
        # `text`; main previously read `body`. Matching the pattern alone
        # cannot catch that mismatch - only calling the function does, and it
        # raises IndexError on every parsed bullet when the names disagree.
        # A multi-line fragment mixing both spellings, so the loop is exercised
        # rather than a single match.
        fragment = (
            "## Highlights\n"
            "* **Parsing Improvements:** PHP imports resolve.\n"
            "* **Web Search**: the agent searches.\n"
            "* **CI Speedups:** builds are faster.\n"
        )
        assert extract_bullets(fragment) == [
            "- **Parsing Improvements**: PHP imports resolve.",
            "- **Web Search**: the agent searches.",
        ]

    def test_a_non_feature_bold_in_the_body_is_rejected_like_a_theme(self) -> None:
        """Closes the hole the previous version of this test pinned (issue #1608).

        `is_feature_theme` was passed the theme and nothing else, so a
        non-feature word in a SECOND bold in the body was never inspected:
        "- **Improvements**: to **CI automation**: ..." carried CI content into
        the README. A bold in the body is a theme in all but position and is
        now held to the same rule. Prose is deliberately not: four current
        NEWS.md entries say "test suite", "type-test patterns", "this release"
        or "benchmark" in passing and must keep passing; nor is inline code,
        where two `**kwargs`-style tokens would otherwise pair into a bold.
        """
        # The theme group cannot end at a LATER bold (anchored to the first).
        assert extract_bullets("- **Improvements** to **CI automation**: x.") == []
        assert extract_bullets("* **Improvements** to **CI automation**: x.") == []
        # The colon-after-first-bold twin, which used to leak.
        assert extract_bullets("- **Improvements**: to **CI automation**: x.") == []
        assert extract_bullets("* **Improvements:** to **CI automation**: x.") == []
        # A feature-named bold in the body is fine.
        assert extract_bullets("- **Improvements**: to **Web Search** ranking.") == [
            "- **Improvements**: to **Web Search** ranking."
        ]
        # Prose mentions of non-feature words are not themes and stay.
        prose = (
            "- **Runtime Call Tracing**: runs your code (typically the test suite) "
            "and merges the calls, handling type-test patterns; this release "
            "adds a benchmark harness."
        )
        assert extract_bullets(prose) == [prose]
        code = "- **Python Taint**: follows `**kwargs` through tests and `**args` too."
        assert extract_bullets(code) == [code]
        # A double-backtick span is one span; stripping backtick pairs
        # separately would leave `**CI**` behind as a bold (#1748 review).
        double = "- **Web Search**: renders ``**CI**`` literally in results."
        assert extract_bullets(double) == [double]
        assert body_themes_are_features("``**docs**`` shown as code") is True
        assert is_feature_theme("Improvements") is True
        assert is_feature_theme("CI automation") is False
        assert body_themes_are_features("plain tests and docs in prose") is True
        assert body_themes_are_features("see **docs** for more") is False

    def test_both_extractors_accept_a_missing_space_after_the_colon(self) -> None:
        """The unification issue #1609 asked for, in the permissive direction.

        This test previously PINNED the disagreement: `BULLET_PATTERN`
        required a space, `HIGHLIGHT_BULLET` accepted none, and it said any
        future unification had to change it deliberately. This is that
        deliberate change.

        The direction was chosen by measuring both, not by symmetry. Making
        both STRICT is worse than the bug: a lone no-space bullet currently
        survives demoted into the Release Summary aggregate, and under a
        strict rule `prepend_news` inserts NOTHING at all, so the feature is
        lost completely rather than misfiled. Permissive is the only rule
        that preserves the content in both arrangements, and the module's
        own docstring notes the generator is not under our control, so
        tolerating its whitespace is the fail-safe direction.
        """
        for line in ("- **Theme:**No space here.", "- **Theme**:No space here."):
            assert extract_bullets(line) == ["- **Theme**: No space here."], line
        for line in ("* **Theme:**No space here.", "* **Theme**:No space here."):
            assert extract_all_highlights(line) == [("Theme", "No space here.")], line

    def test_a_no_space_bullet_reaches_latest_news_as_its_own_entry(self) -> None:
        """The user-visible half of #1609, in both arrangements it measured.

        Before: alone it was demoted to `Release Summary`; beside a
        well-formed sibling it vanished entirely, because `extract_bullets`
        returned non-empty and the aggregate fallback never fired.
        """
        news = "# News\n\n## Latest News\n\n"

        _alone, added_alone = prepend_news(
            news, "## Highlights\n* **Voice Input**:Speak to the agent.\n"
        )
        _pair, added_pair = prepend_news(
            news,
            "## Highlights\n"
            "* **Voice Input**:Speak to the agent.\n"
            "* **Web Search**: Search the web.\n",
        )

        assert added_alone == ["- **Voice Input**: Speak to the agent."]
        assert "- **Voice Input**: Speak to the agent." in added_pair, added_pair
        assert "- **Web Search**: Search the web." in added_pair, added_pair

    def test_a_tab_after_the_colon_is_accepted_by_both_extractors(self) -> None:
        """The same disagreement as #1609, on a different whitespace character.

        The first fix unified the QUANTIFIER (` +` to ` *`) and left the
        CHARACTER CLASS split: a literal space here, `\\s` in
        `HIGHLIGHT_BULLET`. So `- **Theme**:\\tText` still parsed in one
        extractor and not the other, with the same user-visible consequence
        the issue measured. Fixing the named instance rather than the class
        (CodeRabbit on PR #1627).

        Both patterns now share one whitespace class, so a future divergence
        has to be written deliberately in one place rather than drifting.
        """
        assert extract_bullets("- **Theme**:\tText here.") == [
            "- **Theme**: Text here."
        ]
        assert extract_all_highlights("* **Theme**:\tText here.") == [
            ("Theme", "Text here.")
        ]

    def test_a_space_after_the_colon_is_still_normalised_away(self) -> None:
        """The control: loosening must not change the well-formed case.

        Without this, a change that simply stopped normalising whitespace
        would satisfy the assertions above while altering every existing
        bullet.
        """
        assert extract_bullets("- **Theme**:   Lots of space.") == [
            "- **Theme**: Lots of space."
        ]

    def test_the_theme_key_never_keeps_a_trailing_colon(self) -> None:
        # existing_themes() dedupes NEW entries against NEWS.md using the
        # captured theme. If "**X:**" yielded "X:" it would never equal the
        # "X" already in NEWS.md, so every release would re-insert its own
        # entries - the same defect class one step downstream.
        # Asserted on the dedup KEY rather than on prepend_news's output:
        # main's aggregation fallback synthesises a "Release Improvements"
        # entry when nothing fresh survives, so a no-op is no longer the
        # observable behaviour of a fully-deduped fragment.
        news = "# Latest News\n\n- **Ruby Support**: already here.\n"
        assert existing_themes(news) == {"ruby support"}
        colon_inside = extract_bullets("- **Ruby Support:** duplicate attempt.")
        assert colon_inside == ["- **Ruby Support**: duplicate attempt."]
        # The captured theme must equal the key already in NEWS.md, or every
        # release would re-insert its own entries.
        assert existing_themes(colon_inside[0]) == existing_themes(news)

    def test_the_aggregation_fallback_sees_colon_inside_highlights(self) -> None:
        # extract_all_highlights feeds the no-features-passed aggregation, so
        # a blind pattern there means an empty aggregated entry rather than a
        # missing one - a different symptom of the same parse failure.
        assert extract_all_highlights("* **CI Speedups:** faster builds.") == [
            ("CI Speedups", "faster builds.")
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


class TestParseFailureIsReported:
    """`main` must distinguish "nothing to say" from "could not parse it".

    The v0.0.820 outage was silent precisely because these two collapsed into
    one exit-0 path: every highlight used a colon spelling the parser rejected,
    zero bullets were inserted, and the release step reported success. An empty
    highlights file is a legitimate no-op and must stay exit 0, so the failure
    signal keys on bullet-shaped input that parsed to nothing.
    """

    @staticmethod
    def _run(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fragment: str
    ) -> tuple[int, str]:
        """Drive main() end to end against a throwaway NEWS.md."""
        news_path = tmp_path / "NEWS.md"
        news_path.write_text(NEWS, encoding="utf-8")
        fragment_path = tmp_path / "highlights.md"
        fragment_path.write_text(fragment, encoding="utf-8")
        monkeypatch.setattr(update_news, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["update_news.py", str(fragment_path)])
        return update_news.main(), news_path.read_text(encoding="utf-8")

    def test_bullet_shaped_input_that_parses_to_nothing_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The v0.0.820 shape itself: bullets present, none parseable."""
        fragment = (
            "## Highlights\n"
            "- **Web Search**  the agent can now search the web.\n"
            "- **Graph Export**  the graph exports to GraphML.\n"
        )
        exit_code, news = self._run(tmp_path, monkeypatch, fragment)
        captured = capsys.readouterr()
        assert exit_code != 0
        assert "Web Search" not in news
        assert "parsed 0" in captured.out
        # The diagnostic must name the regex an operator has to inspect, and the
        # file to inspect it against. An earlier review found this message naming
        # BULLET_PATTERN after the guard had moved to HIGHLIGHT_BULLET, which would
        # send someone to the wrong regex mid-incident; nothing asserted it then.
        assert "HIGHLIGHT_BULLET" in captured.err
        assert str(tmp_path / "highlights.md") in captured.err

    def test_an_empty_highlights_file_stays_a_clean_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A release with intentionally empty highlights must still bump."""
        exit_code, news = self._run(tmp_path, monkeypatch, "")
        assert exit_code == 0
        assert news == NEWS

    def test_prose_without_bullets_stays_a_clean_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No bullet-shaped line means there was nothing to parse, not a failure."""
        fragment = "## Highlights\n\nThis release contains internal changes only.\n"
        exit_code, news = self._run(tmp_path, monkeypatch, fragment)
        assert exit_code == 0
        assert news == NEWS

    def test_a_fully_deduped_release_is_not_a_parse_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Bullets parsed but every theme is already present: success, not failure.

        This is the case a naive "inserted == 0 means failure" check would call
        a failure, which would turn every workflow rerun red.
        """
        fragment = "- **Ruby Support**: Ruby joins the graph through ast-grep.\n"
        exit_code, news = self._run(tmp_path, monkeypatch, fragment)
        assert exit_code == 0
        assert news == NEWS
        out = capsys.readouterr().out
        assert "parsed 1" in out
        assert "inserted 0" in out

    def test_counts_are_reported_separately(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """parsed/deduped/inserted are distinct numbers, not one collapsed total."""
        fragment = (
            "- **Ruby Support**: already in NEWS, so this one dedupes away.\n"
            "- **Graph Export**: the graph exports to GraphML.\n"
        )
        exit_code, _ = self._run(tmp_path, monkeypatch, fragment)
        assert exit_code == 0
        out = capsys.readouterr().out
        assert "parsed 2" in out
        assert "deduped 1" in out
        assert "inserted 1" in out

    def test_the_aggregation_fallback_is_labelled_not_counted_as_dedup(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An aggregated summary is inserted without ever being "parsed".

        prepend_news falls back to one "Release Summary" bullet when every
        theme is filtered out, and extract_bullets never produced it, so
        inserted exceeds parsed. Reporting that as negative dedup - or
        clamping it to zero and printing "parsed 0, deduped 0, inserted 1" -
        describes a state that cannot happen; the line must say which path ran.
        """
        fragment = (
            "* **CI Speedups**: builds are faster.\n* **Docs**: readme rewritten.\n"
        )
        exit_code, news = self._run(tmp_path, monkeypatch, fragment)
        assert exit_code == 0
        assert "- **Release Summary**:" in news
        out = capsys.readouterr().out
        assert "parsed 0, deduped 0, inserted 1 (aggregated fallback)" in out

    def test_a_rerun_of_an_all_nonfeature_release_is_not_a_parse_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Filtered out is not the same as failed to parse.

        When every theme is non-feature, extract_bullets is empty because
        is_feature_theme dropped them, not because the patterns failed. The
        first run is masked: the aggregation fallback inserts a summary, so the
        guard is never reached. On a rerun that summary is already in NEWS.md,
        nothing is inserted, and a guard keyed on extract_bullets fires with
        "the format has diverged" about bullets that parsed perfectly. The
        parse-success question is what extract_all_highlights answers.
        """
        fragment = (
            "* **CI Automation**: pipelines are faster.\n"
            "* **Docs**: the readme was rewritten.\n"
        )
        first_code, after_first = self._run(tmp_path, monkeypatch, fragment)
        assert first_code == 0
        assert "- **Release Summary**:" in after_first

        # Rerun against the NEWS.md the first run produced.
        (tmp_path / "NEWS.md").write_text(after_first, encoding="utf-8")
        fragment_path = tmp_path / "highlights.md"
        fragment_path.write_text(fragment, encoding="utf-8")
        monkeypatch.setattr(update_news, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(sys, "argv", ["update_news.py", str(fragment_path)])
        assert update_news.main() == 0

    def test_unparsable_and_merely_filtered_are_distinguished(self) -> None:
        """The guard must separate the two ways extract_bullets returns [].

        A direct unit check on the discrimination itself, so a future change
        that reintroduces the conflation fails here rather than only in the
        end-to-end rerun above.
        """
        parses_but_filtered = (
            "* **CI Automation**: pipelines are faster.\n* **Docs**: rewritten.\n"
        )
        does_not_parse = "- **Web Search**  no colon at all.\n"

        assert extract_bullets(parses_but_filtered) == []
        assert extract_bullets(does_not_parse) == []
        assert has_unparsable_bullets(parses_but_filtered) is False
        assert has_unparsable_bullets(does_not_parse) is True
