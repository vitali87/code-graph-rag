import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from codebase_rag.mcp.server import get_project_root


class TestGetProjectRoot:
    """Test suite for get_project_root() function."""

    def test_uses_environment_variable_when_set(self, tmp_path: Path) -> None:
        """Test that TARGET_REPO_PATH environment variable takes priority."""
        test_path = tmp_path / "test_repo"
        test_path.mkdir()

        with patch.dict(os.environ, {"TARGET_REPO_PATH": str(test_path)}):
            result = get_project_root()

        assert result == test_path.resolve()

    def test_uses_settings_when_env_not_set(self, tmp_path: Path) -> None:
        """Test that settings.TARGET_REPO_PATH is used when env var is not set."""
        test_path = tmp_path / "settings_repo"
        test_path.mkdir()

        with patch.dict(os.environ, {}, clear=False):
            if "TARGET_REPO_PATH" in os.environ:
                del os.environ["TARGET_REPO_PATH"]

            with patch("codebase_rag.mcp.server.settings") as mock_settings:
                mock_settings.TARGET_REPO_PATH = str(test_path)
                result = get_project_root()

        assert result == test_path.resolve()

    def test_defaults_to_cwd_when_not_configured(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Test that current working directory is used when TARGET_REPO_PATH is not set."""
        test_cwd = tmp_path / "current_dir"
        test_cwd.mkdir()

        monkeypatch.chdir(test_cwd)
        monkeypatch.setenv("PWD", str(test_cwd))

        if "TARGET_REPO_PATH" in os.environ:
            monkeypatch.delenv("TARGET_REPO_PATH")

        with patch("codebase_rag.mcp.server.settings") as mock_settings:
            mock_settings.TARGET_REPO_PATH = None
            result = get_project_root()

        assert result == test_cwd.resolve()

    def test_defaults_to_cwd_when_empty_string(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Test that empty string in settings falls back to cwd."""
        test_cwd = tmp_path / "current_dir"
        test_cwd.mkdir()

        monkeypatch.chdir(test_cwd)
        monkeypatch.setenv("PWD", str(test_cwd))

        if "TARGET_REPO_PATH" in os.environ:
            monkeypatch.delenv("TARGET_REPO_PATH")

        with patch("codebase_rag.mcp.server.settings") as mock_settings:
            mock_settings.TARGET_REPO_PATH = ""
            result = get_project_root()

        assert result == test_cwd.resolve()

    def test_env_var_takes_priority_over_settings(self, tmp_path: Path) -> None:
        """Test that environment variable takes priority over settings."""
        env_path = tmp_path / "env_repo"
        settings_path = tmp_path / "settings_repo"
        env_path.mkdir()
        settings_path.mkdir()

        with patch.dict(os.environ, {"TARGET_REPO_PATH": str(env_path)}):
            with patch("codebase_rag.mcp.server.settings") as mock_settings:
                mock_settings.TARGET_REPO_PATH = str(settings_path)
                result = get_project_root()

        assert result == env_path.resolve()

    def test_raises_error_when_path_does_not_exist(self) -> None:
        """Test that ValueError is raised when the path does not exist."""
        nonexistent_path = "/path/that/does/not/exist/at/all"

        with patch.dict(os.environ, {"TARGET_REPO_PATH": nonexistent_path}):
            with pytest.raises(
                ValueError, match="Target repository path does not exist"
            ):
                get_project_root()

    def test_raises_error_when_path_is_file(self, tmp_path: Path) -> None:
        """Test that ValueError is raised when the path is a file, not a directory."""
        test_file = tmp_path / "test_file.txt"
        test_file.write_text(encoding="utf-8", data="test content")

        with patch.dict(os.environ, {"TARGET_REPO_PATH": str(test_file)}):
            with pytest.raises(
                ValueError, match="Target repository path is not a directory"
            ):
                get_project_root()

    def test_resolves_relative_paths(self, tmp_path: Path, monkeypatch: Any) -> None:
        """Test that relative paths are resolved to absolute paths."""
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)

        monkeypatch.chdir(parent)

        with patch.dict(os.environ, {"TARGET_REPO_PATH": "./child"}):
            result = get_project_root()

        assert result == child.resolve()
        assert result.is_absolute()

    @pytest.mark.skipif(
        os.name == "nt", reason="Symlinks require special privileges on Windows"
    )
    def test_handles_symlinks(self, tmp_path: Path) -> None:
        """Test that symlinks are resolved correctly."""
        real_path = tmp_path / "real_repo"
        real_path.mkdir()
        symlink_path = tmp_path / "symlink_repo"
        symlink_path.symlink_to(real_path)

        with patch.dict(os.environ, {"TARGET_REPO_PATH": str(symlink_path)}):
            result = get_project_root()

        assert result == real_path.resolve()

    def test_defaults_to_cwd_without_error(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Test that defaulting to cwd works correctly without raising errors."""
        test_cwd = tmp_path / "current_dir"
        test_cwd.mkdir()
        monkeypatch.chdir(test_cwd)
        monkeypatch.setenv("PWD", str(test_cwd))

        if "TARGET_REPO_PATH" in os.environ:
            monkeypatch.delenv("TARGET_REPO_PATH")

        with patch("codebase_rag.mcp.server.settings") as mock_settings:
            mock_settings.TARGET_REPO_PATH = None
            result = get_project_root()

        assert result == test_cwd.resolve()
        assert result.exists()
        assert result.is_dir()

    def test_works_with_actual_cwd(self) -> None:
        """Integration test: verify it works with the actual current working directory."""
        actual_cwd = Path.cwd()

        with patch.dict(os.environ, {}, clear=False):
            if "TARGET_REPO_PATH" in os.environ:
                del os.environ["TARGET_REPO_PATH"]

            with patch("codebase_rag.mcp.server.settings") as mock_settings:
                mock_settings.TARGET_REPO_PATH = None
                result = get_project_root()

        assert result == actual_cwd.resolve()
        assert result.exists()
        assert result.is_dir()
