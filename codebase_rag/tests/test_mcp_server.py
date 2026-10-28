import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from codebase_rag.mcp import server as srv
from codebase_rag.mcp.server import get_project_root


class TestGetProjectRoot:
    """get_project_root() reads settings.TARGET_REPO_PATH, which pydantic
    fills from the environment; "." (the default) is the launch directory."""

    @staticmethod
    def _configure(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setattr(srv.settings, "TARGET_REPO_PATH", value)

    def test_uses_the_configured_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_path = tmp_path / "settings_repo"
        test_path.mkdir()
        self._configure(monkeypatch, str(test_path))

        assert get_project_root() == test_path.resolve()

    def test_the_settings_value_is_the_only_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The environment was read a second time, live, over the snapshot
        # that pydantic had already taken from it.
        env_path = tmp_path / "env_repo"
        settings_path = tmp_path / "settings_repo"
        env_path.mkdir()
        settings_path.mkdir()
        monkeypatch.setenv("TARGET_REPO_PATH", str(env_path))
        self._configure(monkeypatch, str(settings_path))

        assert get_project_root() == settings_path.resolve()

    def test_the_default_is_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_cwd = tmp_path / "current_dir"
        test_cwd.mkdir()
        monkeypatch.chdir(test_cwd)
        self._configure(monkeypatch, ".")

        result = get_project_root()

        assert result == test_cwd.resolve()
        assert result.is_dir()

    def test_raises_error_when_path_does_not_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._configure(monkeypatch, "/path/that/does/not/exist/at/all")
        with pytest.raises(ValueError, match="Target repository path does not exist"):
            get_project_root()

    def test_raises_error_when_path_is_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_file = tmp_path / "test_file.txt"
        test_file.write_text(encoding="utf-8", data="test content")
        self._configure(monkeypatch, str(test_file))
        with pytest.raises(
            ValueError, match="Target repository path is not a directory"
        ):
            get_project_root()

    def test_resolves_relative_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = tmp_path / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        monkeypatch.chdir(parent)
        self._configure(monkeypatch, "./child")

        result = get_project_root()

        assert result == child.resolve()
        assert result.is_absolute()

    @pytest.mark.skipif(
        os.name == "nt", reason="Symlinks require special privileges on Windows"
    )
    def test_handles_symlinks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_path = tmp_path / "real_repo"
        real_path.mkdir()
        symlink_path = tmp_path / "symlink_repo"
        symlink_path.symlink_to(real_path)
        self._configure(monkeypatch, str(symlink_path))

        assert get_project_root() == real_path.resolve()


class TestServiceLifecycle:
    """Tests that the MCP server lifecycle releases the Qdrant client."""

    def test_service_lifecycle_closes_qdrant_on_exit(self) -> None:
        mock_ingestor = MagicMock()

        with patch.object(srv, "close_qdrant_client") as mock_close:
            with srv._service_lifecycle(mock_ingestor):
                mock_ingestor.__enter__.assert_called_once()
                mock_close.assert_not_called()
            mock_close.assert_called_once_with()
            mock_ingestor.__exit__.assert_called_once()

    def test_service_lifecycle_closes_qdrant_on_exception(self) -> None:
        mock_ingestor = MagicMock()

        with patch.object(srv, "close_qdrant_client") as mock_close:
            lifecycle = srv._service_lifecycle(mock_ingestor)
            with pytest.raises(RuntimeError):
                with lifecycle:
                    raise RuntimeError("boom")
            mock_close.assert_called_once_with()
            mock_ingestor.__exit__.assert_called_once()


class TestServeStdioShutdown:
    """Tests that serve_stdio releases the Qdrant lock on shutdown."""

    async def test_serve_stdio_closes_qdrant_client_on_shutdown(self) -> None:
        mock_ingestor = MagicMock()
        mock_server = MagicMock()
        mock_server.run = AsyncMock()
        mock_server.create_initialization_options = MagicMock(return_value=MagicMock())

        @contextlib.asynccontextmanager
        async def fake_stdio() -> AsyncIterator[tuple[MagicMock, MagicMock]]:
            yield (MagicMock(), MagicMock())

        with patch.object(
            srv, "create_server", return_value=(mock_server, mock_ingestor)
        ):
            with patch.object(srv, "stdio_server", fake_stdio):
                with patch.object(srv, "close_qdrant_client") as mock_close:
                    await srv.serve_stdio()

        mock_close.assert_called_once_with()
        mock_server.run.assert_awaited_once()
