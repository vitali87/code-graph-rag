from __future__ import annotations

import json
import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.checkout_state import state_file
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
        mock_ingestor.list_project_roots.return_value = {
            "other": "/repos/other",
            "platform": "/repos/platform",
        }
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
    mock_delete_embeddings.assert_called_once_with("platform", [1, 2], [])


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_leaves_a_nested_projects_embeddings(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    # `platform.` also prefixes `platform.v2`, a project of its own.
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.list_projects.return_value = ["platform", "platform.v2", "platform2"]

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    mock_delete_embeddings.assert_called_once_with("platform", [1, 2], ["platform.v2"])


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
    cache_path = state_file(tmp_path, cs.HASH_CACHE_FILENAME)
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
    cache_path = state_file(tmp_path, cs.HASH_CACHE_FILENAME)
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


# A project's glosses outlive the delete's traversal by design, so the
# deliberate delete sweeps them, as MCP `delete_project` does (issue #3231).


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_sweeps_the_projects_glosses_after_the_delete(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    from codebase_rag.services.gloss_cleanup import CYPHER_DELETE_ORPHANED_GLOSSES

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    ingestor = _get_ingestor(mock_memgraph_connect)
    sweep = call.execute_write(
        CYPHER_DELETE_ORPHANED_GLOSSES,
        {cs.KEY_PROJECT_NAME: "platform", cs.KEY_PROJECT_PREFIX: "platform."},
    )
    # The delete also clears the project's run markers with a write of its
    # own, so the sweep is counted among the writes rather than alone.
    assert ingestor.mock_calls.count(sweep) == 1
    # Before the delete every note is still attached, so a sweep then would
    # find nothing to remove.
    calls = ingestor.mock_calls
    assert calls.index(call.delete_project("platform")) < calls.index(sweep)
    warning = cs.CLI_WARN_GLOSSES_NOT_PRUNED.format(project_name="platform")
    assert warning not in " ".join(_strip_ansi(result.output).split())


@patch("codebase_rag.cli.delete_project_embeddings")
def test_a_failed_gloss_sweep_warns_and_keeps_the_completed_delete(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    # The delete cannot be undone, so a failed sweep is reported, not
    # turned into a failed delete.
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.execute_write.side_effect = RuntimeError("connection reset")

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    output = " ".join(_strip_ansi(result.output).split())
    assert cs.CLI_WARN_GLOSSES_NOT_PRUNED.format(project_name="platform") in output
    assert "Project 'platform' deleted successfully." in output


@patch("codebase_rag.cli.delete_project_embeddings")
def test_a_missing_project_sweeps_nothing(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    # Negative: no delete, no sweep.
    result = runner.invoke(app, ["delete-project", "--name", "ghost"])

    assert result.exit_code == 1
    _get_ingestor(mock_memgraph_connect).execute_write.assert_not_called()


def _gloss_sweep(project_name: str) -> object:
    from codebase_rag.services.gloss_cleanup import CYPHER_DELETE_ORPHANED_GLOSSES

    return call.execute_write(
        CYPHER_DELETE_ORPHANED_GLOSSES,
        {
            cs.KEY_PROJECT_NAME: project_name,
            cs.KEY_PROJECT_PREFIX: f"{project_name}.",
        },
    )


@patch("codebase_rag.cli.delete_project_embeddings")
def test_a_delete_that_fails_after_removing_the_project_still_sweeps(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    # The delete's statements commit one by one: the project went, then a
    # later cleanup raised. A retry stops at "project not found", so this
    # run is the only one that can sweep the notes.
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.list_projects.side_effect = [["platform", "other"], ["other"]]
    ingestor.delete_project.side_effect = RuntimeError("prune failed")

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 1
    assert ingestor.mock_calls.count(_gloss_sweep("platform")) == 1


@patch("codebase_rag.cli.delete_project_embeddings")
def test_a_delete_that_fails_with_the_project_still_listed_sweeps_nothing(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    # Negative: the project is still there, and its unattached notes are
    # LOST or AMBIGUOUS by design, readable on their old names.
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.delete_project.side_effect = RuntimeError("connection reset")

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 1
    assert _gloss_sweep("platform") not in ingestor.mock_calls


def _shared_root_warning(project_name: str, projects: str, root_path: str) -> str:
    return cs.CLI_WARN_DELETE_PROJECT_SHARED_ROOT.format(
        projects=projects, project_name=project_name, root_path=root_path
    )


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_warns_about_projects_indexed_from_the_same_directory(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.list_project_roots.return_value = {
        "other": "/repos/other",
        "platform": "/repos/shop",
        "shop": "/repos/shop/",
        "shop-old": "/repos/shop",
    }

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    stripped = " ".join(_strip_ansi(result.output).split())
    expected = _shared_root_warning("platform", "shop, shop-old", "/repos/shop")
    assert expected in stripped
    ingestor.delete_project.assert_called_once_with("platform")


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_does_not_warn_when_the_directory_is_its_own(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    assert "same directory" not in _strip_ansi(result.output)


@patch("codebase_rag.cli.delete_project_embeddings")
def test_delete_project_does_not_count_projects_without_a_root_as_sharing(
    mock_delete_embeddings: MagicMock,
    mock_memgraph_connect: MagicMock,
) -> None:
    ingestor = _get_ingestor(mock_memgraph_connect)
    ingestor.list_project_roots.return_value = {"legacy": None, "platform": None}

    result = runner.invoke(app, ["delete-project", "--name", "platform"])

    assert result.exit_code == 0, result.output
    assert "same directory" not in _strip_ansi(result.output)
    ingestor.delete_project.assert_called_once_with("platform")
