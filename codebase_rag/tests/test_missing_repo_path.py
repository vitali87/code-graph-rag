"""A run rooted at a path that does not exist must fail, not succeed at nothing.

`GraphUpdater.__init__` recognises a single-file target only while the file
exists (`repo_path.is_file()`), so a path that was deleted or mistyped fell
through as an ordinary directory run rooted at a non-directory. The walk found
no files and `run()` returned normally: a caller asking to re-index one file
got the same success signal whether the path was indexed, deleted or
misspelled (issue #1651).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    return temp_repo


def _create_graph_updater(target: Path, ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor, repo_path=target, parsers=parsers, queries=queries
    )


def test_a_run_rooted_at_a_deleted_file_raises(
    py_project: Path, mock_ingestor: MagicMock
) -> None:
    """The issue's own reproduction: full build, delete a module, run on it.

    Before the fix the second run completed without raising and rewrote
    nothing; the caller could not tell that from a successful re-index.
    """
    _create_graph_updater(py_project, mock_ingestor).run()
    before = (py_project / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
    target = py_project / "module_a.py"
    target.unlink()
    assert not target.exists(), "fixture guard: the target must be gone"

    updater = _create_graph_updater(target, mock_ingestor)
    with pytest.raises(FileNotFoundError, match="module_a.py"):
        updater.run()

    # Nothing was published for the run that never happened.
    after = (py_project / cs.HASH_CACHE_FILENAME).read_text(encoding="utf-8")
    assert after == before, "a run that indexed nothing rewrote the cache"


def test_a_run_rooted_at_a_missing_directory_raises(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    missing = tmp_path / "no_such_project"
    updater = _create_graph_updater(missing, mock_ingestor)
    with pytest.raises(FileNotFoundError, match="no_such_project"):
        updater.run()
    assert not mock_ingestor.ensure_node_batch.called, (
        "a run rooted at a missing directory wrote to the graph"
    )


def test_construction_alone_does_not_require_the_path(
    tmp_path: Path, mock_ingestor: MagicMock
) -> None:
    """The check lives in `run`, not the constructor.

    Unit tests across the suite build updaters on paths such as `/test` to
    exercise parsers and registries without ever running; the constructor
    stays cheap and side-effect free for them.
    """
    _create_graph_updater(tmp_path / "never_run", mock_ingestor)


def test_the_controls_still_run(py_project: Path, mock_ingestor: MagicMock) -> None:
    """A directory and an existing single file both run as before."""
    _create_graph_updater(py_project, mock_ingestor).run()
    assert mock_ingestor.ensure_node_batch.called
    mock_ingestor.reset_mock()
    single = _create_graph_updater(py_project / "module_a.py", mock_ingestor)
    assert single._single_file is not None, "fixture guard: not a single-file run"
    single.run()
    assert mock_ingestor.ensure_node_batch.called
