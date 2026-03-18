from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app

runner = CliRunner()


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _get_ingestor(mock_connect: MagicMock) -> MagicMock:
    return mock_connect.return_value.__enter__.return_value


class TestCleanWithoutUpdateGraph:
    def test_clean_alone_wipes_database(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            app,
            ["start", "--clean", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        ingestor = _get_ingestor(mock_memgraph_connect)
        ingestor.clean_database.assert_called_once()

    def test_clean_alone_deletes_hash_cache(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_path = tmp_path / cs.HASH_CACHE_FILENAME
        cache_path.write_text(json.dumps({"file.py": "abc123"}))

        result = runner.invoke(
            app,
            ["start", "--clean", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        assert not cache_path.exists()

    def test_clean_alone_no_cache_file_still_succeeds(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        cache_path = tmp_path / cs.HASH_CACHE_FILENAME
        assert not cache_path.exists()

        result = runner.invoke(
            app,
            ["start", "--clean", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output

    def test_clean_alone_does_not_invoke_graph_updater(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        with patch("codebase_rag.cli.GraphUpdater") as mock_updater:
            result = runner.invoke(
                app,
                ["start", "--clean", "--repo-path", str(tmp_path)],
            )

        assert result.exit_code == 0, result.output
        mock_updater.assert_not_called()

    def test_clean_alone_shows_clean_done_message(
        self,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            app,
            ["start", "--clean", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0
        assert cs.CLI_MSG_CLEAN_DONE in result.output


class TestCleanWithUpdateGraph:
    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_clean_with_update_deletes_hash_cache(
        self,
        mock_cgrignore: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        from codebase_rag.config import CgrignorePatterns

        mock_cgrignore.return_value = CgrignorePatterns(
            exclude=frozenset(), unignore=frozenset()
        )

        cache_path = tmp_path / cs.HASH_CACHE_FILENAME
        cache_path.write_text(json.dumps({"file.py": "abc123"}))

        result = runner.invoke(
            app,
            ["start", "--clean", "--update-graph", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        assert not cache_path.exists()

    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_clean_with_update_calls_clean_database(
        self,
        mock_cgrignore: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        from codebase_rag.config import CgrignorePatterns

        mock_cgrignore.return_value = CgrignorePatterns(
            exclude=frozenset(), unignore=frozenset()
        )

        result = runner.invoke(
            app,
            ["start", "--clean", "--update-graph", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        ingestor = _get_ingestor(mock_memgraph_connect)
        ingestor.clean_database.assert_called_once()

    @patch("codebase_rag.cli.GraphUpdater")
    @patch("codebase_rag.cli.load_parsers", return_value=({}, {}))
    @patch("codebase_rag.cli.load_cgrignore_patterns")
    def test_update_without_clean_preserves_hash_cache(
        self,
        mock_cgrignore: MagicMock,
        mock_load_parsers: MagicMock,
        mock_graph_updater: MagicMock,
        mock_memgraph_connect: MagicMock,
        tmp_path: Path,
    ) -> None:
        from codebase_rag.config import CgrignorePatterns

        mock_cgrignore.return_value = CgrignorePatterns(
            exclude=frozenset(), unignore=frozenset()
        )

        cache_path = tmp_path / cs.HASH_CACHE_FILENAME
        cache_data = {"file.py": "abc123"}
        cache_path.write_text(json.dumps(cache_data))

        result = runner.invoke(
            app,
            ["start", "--update-graph", "--repo-path", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        assert cache_path.exists()
        assert json.loads(cache_path.read_text()) == cache_data
