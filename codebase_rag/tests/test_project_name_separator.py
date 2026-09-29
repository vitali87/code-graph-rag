"""Issue #2412: a project name never contains the qualified-name separator.

Qualified names are `<project>.<package path>.<module>.<symbol>`, and nodes
MERGE on `qualified_name`. A project named `acme.web` and a project `acme`
with a `web/` package therefore produced the same `acme.web.views` Module:
one node owned by two repositories, carrying whichever path was written last
and DEFINING functions from both. The default name was sanitised; an explicit
`--project-name` was stored verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_check import indexed_scope
from codebase_rag.utils.path_utils import derive_project_name, project_name_error
from codebase_rag.workspaces import (
    WorkspaceError,
    add_repo,
    create_workspace,
    load_workspace,
)
from codebase_rag.workspaces.models import WorkspaceRepo
from evals.cgr_graph import _StatefulIngestor

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    monkeypatch.setattr(settings, "CGR_HOME", tmp_path / "cgr-home")
    return tmp_path / "cgr-home"


@pytest.fixture
def sync() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli.connect_memgraph") as connect,
        patch("codebase_rag.cli._update_and_validate_models"),
        patch("codebase_rag.cli.main_single_query"),
        patch("codebase_rag.cli._run_graph_sync") as run_graph_sync,
    ):
        connect.return_value.__enter__ = MagicMock(return_value=MagicMock())
        connect.return_value.__exit__ = MagicMock(return_value=False)
        yield run_graph_sync


def _start(repo: Path, *extra: str) -> list[str]:
    return ["start", "--repo-path", str(repo), "--no-start-stack", *extra]


def test_start_refuses_a_dotted_project_name(sync: MagicMock, tmp_path: Path) -> None:
    result = runner.invoke(
        app, _start(tmp_path, "--update-graph", "--project-name", "acme.web")
    )

    assert result.exit_code == 2, result.output
    assert "acme_web" in result.output
    sync.assert_not_called()


def test_the_refusal_also_covers_the_pre_chat_sync(
    sync: MagicMock, tmp_path: Path
) -> None:
    # `cgr start` without --update-graph still syncs before the chat.
    result = runner.invoke(
        app, _start(tmp_path, "--project-name", "acme.web", "--ask-agent", "hi")
    )

    assert result.exit_code == 2, result.output
    sync.assert_not_called()


@pytest.mark.parametrize("name", ["acme", "acme_web", "acme-web", "Acme2"])
def test_a_name_without_the_separator_is_stored_as_given(
    sync: MagicMock, tmp_path: Path, name: str
) -> None:
    # Negative: only the separator is refused, nothing is rewritten.
    result = runner.invoke(
        app, _start(tmp_path, "--update-graph", "--project-name", name)
    )

    assert result.exit_code == 0, result.output
    assert sync.call_args.kwargs["project_name"] == name


def test_the_derived_default_is_unchanged(sync: MagicMock, tmp_path: Path) -> None:
    # Negative: a dotted directory already derives a separator-free name.
    repo = tmp_path / "acme.web"
    repo.mkdir()

    result = runner.invoke(app, _start(repo, "--update-graph"))

    assert result.exit_code == 0, result.output
    assert sync.call_args.kwargs["project_name"] == derive_project_name(repo)


def test_workspace_add_repo_refuses_a_dotted_project_name(tmp_path: Path) -> None:
    create_workspace("mono")

    with pytest.raises(WorkspaceError, match="acme_web"):
        add_repo("mono", str(tmp_path), project_name="acme.web")

    assert load_workspace("mono").repos == []


def test_workspace_add_repo_cli_exits_with_the_reason(tmp_path: Path) -> None:
    runner.invoke(app, ["workspace", "create", "mono"])

    result = runner.invoke(
        app, ["workspace", "add-repo", "mono", str(tmp_path), "-p", "acme.web"]
    )

    assert result.exit_code == 1
    assert "acme_web" in result.output


def test_workspace_add_repo_keeps_a_plain_name(tmp_path: Path) -> None:
    # Negative.
    create_workspace("mono")

    _, repo = add_repo("mono", str(tmp_path), project_name="acme_web")

    assert repo.project_name == "acme_web"


def test_a_workspace_saved_with_a_dotted_name_is_not_synced(
    sync: MagicMock, tmp_path: Path
) -> None:
    # A workspace file written before this fix can still hold a dotted name;
    # syncing it would merge nodes, so nothing in the workspace is written.
    plain, dotted = tmp_path / "plain", tmp_path / "frontend"
    plain.mkdir()
    dotted.mkdir()
    create_workspace(
        "mono",
        repos=[
            WorkspaceRepo(path=str(plain), project_name="acme"),
            WorkspaceRepo(path=str(dotted), project_name="acme.web"),
        ],
    )

    result = runner.invoke(
        app, _start(plain, "--workspace", "mono", "--ask-agent", "hi")
    )

    assert result.exit_code == 1, result.output
    assert "acme.web" in result.output and "acme_web" in result.output
    sync.assert_not_called()


def test_a_workspace_of_plain_names_still_syncs(
    sync: MagicMock, tmp_path: Path
) -> None:
    # Negative.
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    create_workspace("mono")
    add_repo("mono", str(first), project_name="acme")
    add_repo("mono", str(second), project_name="acme_web")

    result = runner.invoke(
        app, _start(first, "--workspace", "mono", "--ask-agent", "hi")
    )

    assert result.exit_code == 0, result.output
    assert sync.call_count == 2


@pytest.mark.parametrize(
    ("name", "suggestion"),
    [("acme.web", "acme_web"), ("a..b.", "a_b"), (".hidden", "hidden")],
)
def test_the_refusal_suggests_a_usable_name(name: str, suggestion: str) -> None:
    error = project_name_error(name)

    assert error is not None and f"'{suggestion}'" in error


@pytest.mark.parametrize("name", ["acme", "acme_web", "my-repo__4bd9a922"])
def test_names_without_the_separator_have_no_error(name: str) -> None:
    # Negative.
    assert project_name_error(name) is None


def _index(root: Path, store: _StatefulIngestor, name: str | None) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=name,
    ).run(force=True)


def _defined_by(store: _StatefulIngestor, module_qn: str) -> set[str]:
    return {
        str(edge[4])
        for edge in store.keyed_edges
        if edge[2] == "DEFINES" and edge[1] == module_qn
    }


def test_a_dotted_directory_does_not_merge_with_another_projects_package(
    tmp_path: Path,
) -> None:
    # `GraphUpdater`'s own default (used by `cgr index`) took the directory
    # name verbatim: a checkout at `acme.web/` wrote the nodes of project
    # `acme`'s package `web`.
    frontend = tmp_path / "acme.web"
    frontend.mkdir()
    (frontend / "views.py").write_text("def render_page():\n    return 1\n")
    monorepo = tmp_path / "monorepo"
    (monorepo / "web").mkdir(parents=True)
    (monorepo / "web" / "__init__.py").write_text("")
    (monorepo / "web" / "views.py").write_text("def start_server():\n    return 2\n")
    store = _StatefulIngestor()

    _index(frontend, store, None)
    _index(monorepo, store, "acme")

    assert _defined_by(store, "acme.web.views") == {"acme.web.views.start_server"}
    assert _defined_by(store, "acme_web.views") == {"acme_web.views.render_page"}


def test_check_reads_the_scope_an_unnamed_run_stamped_on_a_dotted_directory(
    tmp_path: Path,
) -> None:
    # `cgr index` without a name stamps the updater's default and `cgr check`
    # derives the digest-suffixed name; the stamp answers to both spellings
    # of the tree's identity, including the separator-free default.
    root = tmp_path / "acme.web"
    root.mkdir()
    (root / "views.py").write_text("def render_page():\n    return 1\n")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        exclude_paths=frozenset({"gen"}),
    ).run(force=True)

    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"gen"}),
        None,
    )


def test_a_stamp_written_under_the_old_dotted_default_is_still_read(
    tmp_path: Path,
) -> None:
    # Negative: a stamp from before this change carries the verbatim name.
    root = tmp_path / "acme.web"
    root.mkdir()
    (root / cs.EXCLUSION_STATE_FILENAME).write_text(
        json.dumps({"project": "acme.web", "exclude": ["gen"], "unignore": []})
    )

    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"gen"}),
        None,
    )
