"""Every updater a sync builds applies the repository's ignore policy.

The CLI, MCP, the watcher and `cgr check` loaded `.cgrignore` and the root
`.gitignore` themselves; the chat session's refresher, `cgr rename` and the
editing re-ingest built a GraphUpdater without them, so those paths parsed
what the repository excludes and stamped a different exclusion set (#1616).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from codebase_rag.config import CGRIGNORE_FILENAME, project_ignore_sets
from codebase_rag.tools.graph_refresh import GraphRefresher


def _repo(tmp_path: Path) -> Path:
    (tmp_path / CGRIGNORE_FILENAME).write_text("generated/\n", encoding="utf-8")
    return tmp_path


def test_the_policy_is_the_repositorys_ignore_files(tmp_path: Path) -> None:
    exclude, _unignore = project_ignore_sets(_repo(tmp_path))
    assert exclude is not None and "generated" in {p.rstrip("/") for p in exclude}


def test_a_repository_without_ignore_files_gets_the_updater_default(
    tmp_path: Path,
) -> None:
    assert project_ignore_sets(tmp_path) == (None, None)


def test_the_chat_refresher_builds_its_updater_with_the_policy(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    refresher = GraphRefresher(MagicMock(), repo, "proj")
    with (
        patch("codebase_rag.tools.graph_refresh.GraphUpdater") as updater_cls,
        patch("codebase_rag.tools.graph_refresh.load_parsers", return_value=({}, {})),
    ):
        refresher._session_updater()
    kwargs = updater_cls.call_args.kwargs
    assert (kwargs["exclude_paths"], kwargs["unignore_paths"]) == project_ignore_sets(
        repo
    )


def test_the_editing_reingest_builds_its_updater_with_the_policy(
    tmp_path: Path,
) -> None:
    from codebase_rag.editing import cli as editing_cli

    repo = _repo(tmp_path)
    ingestor = MagicMock()
    ingestor.list_projects.return_value = ["proj"]
    with (
        patch("codebase_rag.graph_updater.GraphUpdater") as updater_cls,
        patch("codebase_rag.parser_loader.load_parsers", return_value=({}, {})),
    ):
        editing_cli._reingest_restored(ingestor, repo, "proj", ["a.py"])
    kwargs = updater_cls.call_args.kwargs
    assert (kwargs["exclude_paths"], kwargs["unignore_paths"]) == project_ignore_sets(
        repo
    )
