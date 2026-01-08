from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag.config import CGRIGNORE_FILENAME, load_cgrignore_patterns
from codebase_rag.main import prompt_for_unignored_directories


def test_returns_empty_when_no_file(temp_repo: Path) -> None:
    result = load_cgrignore_patterns(temp_repo)
    assert result == frozenset()


def test_loads_patterns_from_file(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nmy_build\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result
    assert "my_build" in result
    assert len(result) == 2


def test_ignores_comments_and_blank_lines(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("# Comment\n\nvendor\n  # Indented comment\n")

    result = load_cgrignore_patterns(temp_repo)

    assert result == frozenset({"vendor"})


def test_strips_whitespace(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("  vendor  \n\ttemp\t\n")

    result = load_cgrignore_patterns(temp_repo)

    assert "vendor" in result
    assert "temp" in result


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
    assert result == frozenset()


def test_handles_duplicates(temp_repo: Path) -> None:
    cgrignore = temp_repo / CGRIGNORE_FILENAME
    cgrignore.write_text("vendor\nvendor\ntemp\n")

    result = load_cgrignore_patterns(temp_repo)

    assert len(result) == 2


def test_returns_empty_if_cgrignore_is_a_directory(temp_repo: Path) -> None:
    cgrignore_path = temp_repo / CGRIGNORE_FILENAME
    cgrignore_path.mkdir()

    result = load_cgrignore_patterns(temp_repo)

    assert result == frozenset()


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
