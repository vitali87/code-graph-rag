from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    EMPTY_CGRIGNORE,
    load_cgrignore_patterns,
)
from codebase_rag.main import prompt_for_unignored_directories
from codebase_rag.types_defs import CgrignorePatterns


def test_returns_empty_when_no_file(temp_repo: Path) -> None:
    result = load_cgrignore_patterns(temp_repo)
    assert result == EMPTY_CGRIGNORE


def test_loads_exclude_patterns_from_file(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nmy_build\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result.exclude
    assert "my_build" in result.exclude
    assert len(result.exclude) == 2
    assert len(result.unignore) == 0


def test_ignores_comments_and_blank_lines(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("# Comment\n\nvendor\n  # Indented comment\n")

    result = load_cgrignore_patterns(temp_repo)

    assert result.exclude == frozenset({"vendor"})
    assert result.unignore == frozenset()


def test_strips_whitespace(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("  vendor  \n\ttemp\t\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result.exclude
    assert "temp" in result.exclude


def test_returns_empty_on_read_error(
    temp_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor")

    original_open = Path.open

    def mock_open(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        if self.name == CGRIGNORE_FILENAME:
            raise PermissionError("Cannot read")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", mock_open)

    result = load_cgrignore_patterns(temp_repo)
    assert result == EMPTY_CGRIGNORE


def test_handles_duplicates(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nvendor\ntemp\n")

    result = load_cgrignore_patterns(temp_repo)

    assert len(result.exclude) == 2


def test_returns_empty_if_cgrignore_is_a_directory(temp_repo: Path) -> None:
    cgrignore_path = temp_repo / CGRIGNORE_FILENAME
    cgrignore_path.mkdir()

    result = load_cgrignore_patterns(temp_repo)

    assert result == EMPTY_CGRIGNORE


class TestNegationSyntax:
    def test_parses_negation_patterns(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text("!vendor\n!node_modules\n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"vendor", "node_modules"})
        assert result.exclude == frozenset()

    def test_mixed_exclude_and_negation(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text("custom_build\n!vendor\ntemp_data\n!node_modules\n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.exclude == frozenset({"custom_build", "temp_data"})
        assert result.unignore == frozenset({"vendor", "node_modules"})

    def test_negation_strips_leading_whitespace(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text("  !vendor  \n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"vendor"})

    def test_negation_strips_whitespace_after_exclamation(
        self, temp_repo: Path
    ) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text("!  foo\n!   bar  \n")

        result = load_cgrignore_patterns(temp_repo)

        assert result.unignore == frozenset({"foo", "bar"})

    def test_returns_cgrignore_patterns_type(self, temp_repo: Path) -> None:
        cgrignore = temp_repo / CGRIGNORE_FILENAME
        cgrignore.write_text("exclude\n!unignore\n")

        result = load_cgrignore_patterns(temp_repo)

        assert isinstance(result, CgrignorePatterns)


class TestCgrignoreIntegration:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_patterns_included_in_candidates(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("vendor\ncustom_cache\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert ".git" in result
        assert "vendor" in result
        assert "custom_cache" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_merged_with_cli_excludes(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("from_cgrignore\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path, cli_excludes=["from_cli"])

        assert "from_cgrignore" in result
        assert "from_cli" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_only_returns_without_prompt_when_empty(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        result = prompt_for_unignored_directories(tmp_path)

        assert result == frozenset()
        mock_ask.assert_not_called()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_alone_triggers_prompt(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("my_custom_dir\n")
        mock_ask.return_value = "none"

        prompt_for_unignored_directories(tmp_path)

        mock_ask.assert_called_once()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_cgrignore_deduplicates_with_detected(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text(".git\nvendor\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert ".git" in result
        assert "vendor" in result
        assert len([x for x in result if x == ".git"]) == 1


class TestNegationIntegration:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_included_when_user_selects_none(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("custom_exclude\n!vendor\n")
        mock_ask.return_value = "none"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert "custom_exclude" not in result
        assert ".git" not in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_merged_with_user_selection(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("!vendor\n")
        mock_ask.return_value = "1"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert ".git" in result or "node_modules" in result

    def test_unignore_only_returns_without_prompt(self, tmp_path: Path) -> None:
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("!vendor\n!node_modules\n")

        result = prompt_for_unignored_directories(tmp_path)

        assert result == frozenset({"vendor", "node_modules"})

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_unignore_included_when_user_selects_all(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cgrignore = tmp_path / CGRIGNORE_FILENAME
        cgrignore.write_text("custom\n!vendor\n")
        mock_ask.return_value = "all"

        result = prompt_for_unignored_directories(tmp_path)

        assert "vendor" in result
        assert ".git" in result
        assert "custom" in result
