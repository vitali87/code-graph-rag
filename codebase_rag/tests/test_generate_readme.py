from __future__ import annotations

from pathlib import Path

from codebase_rag import cli_help as ch
from codebase_rag.readme_sections import format_cli_commands_table, format_latest_news
from scripts.generate_readme import TARGET_FILES, replace_sections, update_file


class TestReplaceSections:
    def test_replaces_marked_section(self) -> None:
        content = "<!-- SECTION:foo -->\nold\n<!-- /SECTION:foo -->"
        result = replace_sections(content, {"foo": "new"})
        assert "new" in result
        assert "old" not in result

    def test_ignores_unknown_section(self) -> None:
        content = "<!-- SECTION:foo -->\nold\n<!-- /SECTION:foo -->"
        result = replace_sections(content, {"bar": "new"})
        assert result == content


class TestDocsTargets:
    def test_docs_pages_are_generated(self) -> None:
        assert "README.md" in TARGET_FILES
        assert "docs/architecture/language-support.md" in TARGET_FILES
        assert "docs/guide/mcp-server.md" in TARGET_FILES

    def test_update_file_rewrites_sections(self, tmp_path: Path) -> None:
        page = tmp_path / "page.md"
        page.write_text(
            "<!-- SECTION:mcp_tools -->\nstale\n<!-- /SECTION:mcp_tools -->",
            encoding="utf-8",
        )
        assert update_file(page, {"mcp_tools": "fresh"}) is True
        updated = page.read_text(encoding="utf-8")
        assert "fresh" in updated
        assert "stale" not in updated

    def test_update_file_skips_write_when_unchanged(self, tmp_path: Path) -> None:
        page = tmp_path / "page.md"
        page.write_text(
            "<!-- SECTION:mcp_tools -->\nfresh\n<!-- /SECTION:mcp_tools -->",
            encoding="utf-8",
        )
        page.chmod(0o444)
        try:
            assert update_file(page, {"mcp_tools": "fresh"}) is False
        finally:
            page.chmod(0o644)


class TestLatestNews:
    def test_renders_top_n_bullets(self, tmp_path: Path) -> None:
        news = tmp_path / "NEWS.md"
        news.write_text(
            "# News\n\n"
            "- **A**: first.\n"
            "- **B**: second.\n"
            "- **C**: third.\n"
            "- **D**: fourth.\n",
            encoding="utf-8",
        )
        result = format_latest_news(news, limit=3)
        assert result == "- **A**: first.\n- **B**: second.\n- **C**: third."

    def test_takes_all_when_fewer_than_limit(self, tmp_path: Path) -> None:
        news = tmp_path / "NEWS.md"
        news.write_text("- **A**: only one.\n", encoding="utf-8")
        assert format_latest_news(news, limit=3) == "- **A**: only one."

    def test_joins_wrapped_continuation_lines(self, tmp_path: Path) -> None:
        news = tmp_path / "NEWS.md"
        news.write_text(
            "- **A**: line one\n  wrapped line two.\n- **B**: second.\n",
            encoding="utf-8",
        )
        assert (
            format_latest_news(news, limit=1)
            == "- **A**: line one\n  wrapped line two."
        )

    def test_blank_line_ends_bullet_and_drops_trailing_prose(
        self, tmp_path: Path
    ) -> None:
        news = tmp_path / "NEWS.md"
        news.write_text(
            "# News\n\n- **A**: only entry.\n\nSee CHANGELOG for more.\n",
            encoding="utf-8",
        )
        assert format_latest_news(news, limit=3) == "- **A**: only entry."

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert format_latest_news(tmp_path / "nope.md") == ""


def test_cli_command_table_has_one_markdown_row_per_command() -> None:
    lines = format_cli_commands_table().splitlines()

    assert len(lines) == len(ch.CLI_COMMANDS) + 2
    assert all(line.startswith("|") and line.endswith("|") for line in lines)
