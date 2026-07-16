from __future__ import annotations

from pathlib import Path

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
        update_file(page, {"mcp_tools": "fresh"})
        updated = page.read_text(encoding="utf-8")
        assert "fresh" in updated
        assert "stale" not in updated
