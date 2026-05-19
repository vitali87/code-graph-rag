from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


runner = CliRunner()


@pytest.fixture
def mock_memgraph_connect() -> Generator[MagicMock, None, None]:
    with patch("codebase_rag.cli.connect_memgraph") as mock_connect:
        mock_ingestor = MagicMock()
        mock_ingestor.list_projects.return_value = ["platform", "other"]
        mock_ingestor.fetch_all.return_value = [
            {cs.KEY_NODE_ID: 1},
            {cs.KEY_NODE_ID: 2},
        ]
        mock_connect.return_value.__enter__ = MagicMock(return_value=mock_ingestor)
        mock_connect.return_value.__exit__ = MagicMock(return_value=False)
        yield mock_connect


def _get_ingestor(mock_connect: MagicMock) -> MagicMock:
    return mock_connect.return_value.__enter__.return_value


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_calls_ingestor_delete_project(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.delete_project.assert_called_once_with("platform")


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_cleans_embeddings_with_node_ids(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    mock_delete_embeddings.assert_called_once_with("platform", [1, 2])


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_fails_when_project_missing(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "ghost"])

    assert result.exit_code == 1
    assert "ghost" in result.output
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.delete_project.assert_not_called()
    mock_delete_embeddings.assert_not_called()


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_rejects_blank_name(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "   "])

    assert result.exit_code == 1
    assert cs.CLI_ERR_PROJECT_NAME_REQUIRED in result.output
    mock_memgraph_connect.assert_not_called()
    mock_delete_embeddings.assert_not_called()


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_removes_hash_cache_when_repo_path_given(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / cs.HASH_CACHE_FILENAME
    cache_path.write_text(json.dumps({"file.py": "abc123"}))

    result = runner.invoke(
        app,
        ["delete-project", "--name", "platform", "--repo-path", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert not cache_path.exists()


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_without_repo_path_leaves_unrelated_hash_caches(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / cs.HASH_CACHE_FILENAME
    cache_path.write_text(json.dumps({"file.py": "abc123"}))

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    assert cache_path.exists()


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_does_not_wipe_other_projects(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.clean_database.assert_not_called()


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_shows_success_message(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    stripped = _strip_ansi(result.output)
    assert cs.CLI_MSG_PROJECT_DELETED.format(project_name="platform") in stripped
