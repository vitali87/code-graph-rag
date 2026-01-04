from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebase_rag import constants as cs
from codebase_rag.main import (
    detect_root_excludable_directories,
    prompt_exclude_directories,
)
from codebase_rag.utils.path_utils import should_skip_path


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


class TestIgnorePatterns:
    def test_site_packages_in_ignore_patterns(self) -> None:
        assert "site-packages" in cs.IGNORE_PATTERNS

    def test_venv_patterns_in_ignore_patterns(self) -> None:
        assert "venv" in cs.IGNORE_PATTERNS
        assert ".venv" in cs.IGNORE_PATTERNS

    def test_detects_site_packages_at_root(self, tmp_path: Path) -> None:
        (tmp_path / "site-packages").mkdir()

        detected = detect_root_excludable_directories(tmp_path)

        assert "site-packages" in detected


class TestNestedDirectoryExclusion:
    def test_skips_nested_site_packages(self, tmp_path: Path) -> None:
        nested_path = (
            tmp_path
            / "notebook-venv"
            / "lib"
            / "python3.12"
            / "site-packages"
            / "some_package"
        )
        nested_path.mkdir(parents=True)
        file_path = nested_path / "module.py"
        file_path.touch()

        exclude_patterns = frozenset({"site-packages"})

        assert should_skip_path(file_path, tmp_path, exclude_patterns)

    def test_skips_deeply_nested_excluded_directory(self, tmp_path: Path) -> None:
        nested_path = tmp_path / "a" / "b" / "c" / "node_modules" / "pkg"
        nested_path.mkdir(parents=True)
        file_path = nested_path / "index.js"
        file_path.touch()

        exclude_patterns = frozenset({"node_modules"})

        assert should_skip_path(file_path, tmp_path, exclude_patterns)

    def test_does_not_skip_without_matching_pattern(self, tmp_path: Path) -> None:
        nested_path = tmp_path / "src" / "lib" / "utils"
        nested_path.mkdir(parents=True)
        file_path = nested_path / "helpers.py"
        file_path.touch()

        exclude_patterns = frozenset({"node_modules", "site-packages"})

        assert not should_skip_path(file_path, tmp_path, exclude_patterns)

    def test_skips_multiple_exclude_patterns(self, tmp_path: Path) -> None:
        paths = [
            tmp_path / "venv" / "lib" / "file.py",
            tmp_path / "project" / "node_modules" / "pkg" / "index.js",
            tmp_path / "env" / "site-packages" / "dep" / "module.py",
        ]
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

        exclude_patterns = frozenset({"venv", "node_modules", "site-packages"})

        for p in paths:
            assert should_skip_path(p, tmp_path, exclude_patterns)

    @pytest.mark.parametrize(
        "path_parts",
        [
            (
                "notebook-venv",
                "lib",
                "python3.12",
                "site-packages",
                "requests",
                "api.py",
            ),
            ("my-env", "lib", "site-packages", "numpy", "core.py"),
            (".venv", "lib", "python3.11", "site-packages", "flask", "app.py"),
        ],
    )
    def test_skips_various_site_packages_paths(
        self, tmp_path: Path, path_parts: tuple[str, ...]
    ) -> None:
        file_path = tmp_path.joinpath(*path_parts)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.touch()

        exclude_patterns = frozenset({"site-packages"})

        assert should_skip_path(file_path, tmp_path, exclude_patterns)
