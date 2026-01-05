from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag import constants as cs
from codebase_rag.main import (
    detect_excludable_directories,
    prompt_exclude_directories,
)
from codebase_rag.utils.path_utils import should_skip_path


class TestDetectExcludableDirectories:
    def test_detects_matching_patterns_at_root(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".git" in detected
        assert "node_modules" in detected
        assert "src" not in detected

    def test_detects_nested_matching_patterns_with_full_path(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "notebook-venv" / "lib" / "python3.12" / "site-packages").mkdir(
            parents=True
        )
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert "notebook-venv/lib/python3.12/site-packages" in detected

    def test_detects_all_nested_patterns(self, tmp_path: Path) -> None:
        (tmp_path / ".venv" / "lib" / "site-packages" / "vendor").mkdir(parents=True)
        (tmp_path / "src").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".venv" in detected
        assert ".venv/lib/site-packages" in detected
        assert ".venv/lib/site-packages/vendor" in detected

    def test_detects_multiple_git_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "submodule1" / ".git").mkdir(parents=True)
        (tmp_path / "submodule2" / ".git").mkdir(parents=True)

        detected = detect_excludable_directories(tmp_path)

        assert ".git" in detected
        assert "submodule1/.git" in detected
        assert "submodule2/.git" in detected

    def test_ignores_files(self, tmp_path: Path) -> None:
        (tmp_path / ".git").touch()
        (tmp_path / "venv").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert ".git" not in detected
        assert "venv" in detected

    def test_empty_repo_returns_empty_set(self, tmp_path: Path) -> None:
        detected = detect_excludable_directories(tmp_path)
        assert detected == set()

    def test_no_matching_patterns(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "lib").mkdir()
        (tmp_path / "tests").mkdir()

        detected = detect_excludable_directories(tmp_path)
        assert detected == set()


class TestPromptExcludeDirectories:
    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_empty_repo_returns_empty(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        result = prompt_exclude_directories(tmp_path)
        assert result == frozenset()
        mock_ask.assert_not_called()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_all_keeps_everything(
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
    def test_prompt_none_keeps_nothing(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "node_modules").mkdir()
        mock_ask.return_value = "none"

        result = prompt_exclude_directories(tmp_path)

        assert result == frozenset()

    @patch("codebase_rag.main.Prompt.ask")
    @patch("codebase_rag.main.app_context")
    def test_prompt_specific_numbers_keeps_selected(
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
    def test_prompt_with_cli_excludes(
        self, mock_context: MagicMock, mock_ask: MagicMock, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        cli_excludes = ["custom"]
        mock_ask.return_value = "all"

        result = prompt_exclude_directories(tmp_path, cli_excludes=cli_excludes)

        assert ".git" in result
        assert "custom" in result


class TestIgnorePatterns:
    def test_site_packages_in_ignore_patterns(self) -> None:
        assert "site-packages" in cs.IGNORE_PATTERNS

    def test_venv_patterns_in_ignore_patterns(self) -> None:
        assert "venv" in cs.IGNORE_PATTERNS
        assert ".venv" in cs.IGNORE_PATTERNS

    def test_detects_site_packages_at_root(self, tmp_path: Path) -> None:
        (tmp_path / "site-packages").mkdir()

        detected = detect_excludable_directories(tmp_path)

        assert "site-packages" in detected


class TestShouldSkipPath:
    def test_skips_path_matching_ignore_patterns(self, tmp_path: Path) -> None:
        file_path = tmp_path / ".git" / "config"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert should_skip_path(file_path, tmp_path)

    def test_skips_nested_ignore_pattern(self, tmp_path: Path) -> None:
        file_path = tmp_path / "pkg" / "__pycache__" / "module.pyc"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert should_skip_path(file_path, tmp_path)

    def test_does_not_skip_normal_path(self, tmp_path: Path) -> None:
        file_path = tmp_path / "src" / "lib" / "utils" / "helpers.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert not should_skip_path(file_path, tmp_path)

    def test_include_paths_overrides_default_skip(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "submodule1" / ".git").mkdir(parents=True)

        file_in_root_git = tmp_path / ".git" / "config"
        file_in_sub1_git = tmp_path / "submodule1" / ".git" / "config"
        for f in [file_in_root_git, file_in_sub1_git]:
            f.touch()

        include_paths = frozenset({"submodule1/.git"})

        assert should_skip_path(file_in_root_git, tmp_path)
        assert not should_skip_path(
            file_in_sub1_git, tmp_path, include_paths=include_paths
        )

    def test_exclude_paths_adds_to_default_skip(self, tmp_path: Path) -> None:
        custom_dir = tmp_path / "my_custom_dir" / "file.txt"
        custom_dir.parent.mkdir(parents=True)
        custom_dir.touch()

        assert not should_skip_path(custom_dir, tmp_path)

        exclude_paths = frozenset({"my_custom_dir"})
        assert should_skip_path(custom_dir, tmp_path, exclude_paths=exclude_paths)

    def test_does_not_match_partial_directory_names(self, tmp_path: Path) -> None:
        file_path = tmp_path / "my-venv-backup" / "file.py"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        assert not should_skip_path(file_path, tmp_path)
