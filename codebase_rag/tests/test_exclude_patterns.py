from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag.main import (
    detect_root_excludable_directories,
    prompt_exclude_directories,
)


class TestDetectRootExcludableDirectories:
    def test_detects_matching_patterns(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "src").mkdir()

        detected = detect_root_excludable_directories(tmp_path)

        assert ".git" in detected
        assert "node_modules" in detected
        assert "src" not in detected

    def test_ignores_files(self, tmp_path: Path) -> None:
        (tmp_path / ".git").touch()
        (tmp_path / "venv").mkdir()

        detected = detect_root_excludable_directories(tmp_path)

        assert ".git" not in detected
        assert "venv" in detected

    def test_empty_repo_returns_empty_set(self, tmp_path: Path) -> None:
        detected = detect_root_excludable_directories(tmp_path)
        assert detected == set()

    def test_no_matching_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "tests").mkdir()

        detected = detect_root_excludable_directories(tmp_path)
        assert detected == set()


class TestPromptExcludeDirectories:
    def test_skip_prompt_returns_all_detected(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()

        result = prompt_exclude_directories(tmp_path, skip_prompt=True)

        assert ".git" in result
        assert "node_modules" in result

    def test_skip_prompt_includes_cli_excludes(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        cli_excludes = ["custom_dir"]

        result = prompt_exclude_directories(
            tmp_path, cli_excludes=cli_excludes, skip_prompt=True
        )

        assert ".git" in result
        assert "custom_dir" in result

    def test_empty_repo_no_cli_excludes_returns_empty(self, tmp_path: Path) -> None:
        result = prompt_exclude_directories(tmp_path, skip_prompt=True)
        assert result == frozenset()

    def test_cli_excludes_only_with_skip_prompt(self, tmp_path: Path) -> None:
        cli_excludes = ["vendor", "build"]

        result = prompt_exclude_directories(
            tmp_path, cli_excludes=cli_excludes, skip_prompt=True
        )

        assert result == frozenset({"vendor", "build"})

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_all_excludes_everything(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        mock_ask.return_value = "all"

        result = prompt_exclude_directories(tmp_path)

        assert ".git" in result
        assert "node_modules" in result

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_none_excludes_nothing(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        mock_ask.return_value = "none"

        result = prompt_exclude_directories(tmp_path)

        assert result == frozenset()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_specific_numbers(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "venv").mkdir()
        mock_ask.return_value = "1,3"

        result = prompt_exclude_directories(tmp_path)

        assert result == frozenset({".git", "venv"})

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_with_cli_preselected(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cli_excludes = ["custom"]
        mock_ask.return_value = "all"

        result = prompt_exclude_directories(tmp_path, cli_excludes=cli_excludes)

        assert ".git" in result
        assert "custom" in result
