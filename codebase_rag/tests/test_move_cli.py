# `cgr move` (issue #1534): argument forwarding, the JSON report, the exit
# status, the refusal path, and the guard that keeps an explicit --project
# from editing a checkout it was not indexed from.

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from typer.testing import CliRunner

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.test_move_op import FIXTURE, PROJECT, _index, _materialise
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = _materialise(temp_repo, FIXTURE)
    store, updater = _index(root)
    return root, store, updater


def _invoke(
    store: _StatefulIngestor, updater: GraphUpdater, repo: Path, *args: str
) -> tuple[int, str, str]:
    with (
        patch(
            "codebase_rag.graph_cli._project_and_fetch",
            return_value=(PROJECT, store.fetch_all, MagicMock()),
        ),
        patch("codebase_rag.cli.GraphUpdater", return_value=updater),
    ):
        result = CliRunner().invoke(
            app, ["move", *args, "--repo-path", str(repo), "--project", PROJECT]
        )
    return result.exit_code, result.stdout, result.stderr


def test_cli_move_dry_run_reports_and_writes_nothing(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    code, out, err = _invoke(
        store, updater, root, f"{PROJECT}.pkg.util.helper", "pkg.core", "--dry-run"
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["applied"] is False
    assert payload["new_qualified_name"] == f"{PROJECT}.pkg.core.helper"
    assert payload["new_path"] == "pkg/core.py"
    assert (root / "pkg/util.py").read_text() == FIXTURE["pkg/util.py"]
    assert not (root / "pkg/core.py").exists()


def test_cli_move_applies_and_reports_the_verdict(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    code, out, err = _invoke(
        store, updater, root, f"{PROJECT}.pkg.util.helper", "pkg.core", "--keep-alias"
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["applied"] is True
    assert payload[cs.KEY_VERDICT]["ok"] is True
    assert "def helper" in (root / "pkg/core.py").read_text()
    assert "from pkg.core import helper" in (root / "pkg/util.py").read_text()


def test_cli_move_refusal_exits_nonzero_on_stderr(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    code, out, err = _invoke(
        store, updater, root, f"{PROJECT}.pkg.util.helper", "pkg.util"
    )
    assert code == 1
    assert out == ""
    assert "already holds" in err


def test_cli_move_refuses_a_project_indexed_from_another_checkout(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater], temp_repo: Path
) -> None:
    root, store, updater = indexed
    other = temp_repo / "other_checkout"
    shutil.copytree(root, other)
    before = {rel: (other / rel).read_text() for rel in FIXTURE}

    code, out, err = _invoke(
        store, updater, other, f"{PROJECT}.pkg.util.helper", "pkg.core"
    )

    assert code == 1
    assert out == ""
    assert cs.MOVE_CLI_WRONG_ROOT.format(project=PROJECT, root=other.resolve()) in err
    for rel, text in before.items():
        assert (other / rel).read_text() == text
    assert not (other / "pkg/core.py").exists()


def test_cli_move_help_is_move_specific() -> None:
    command = typer.main.get_command(app)
    assert isinstance(command, click.Group)
    move = command.commands[ch.CLICommandName.MOVE]
    helps = {param.name: getattr(param, "help", None) for param in move.params}
    assert helps["qualified_name"] == ch.HELP_MOVE_QN
    assert helps["dry_run"] == ch.HELP_MOVE_DRY_RUN
    # Moving is not renaming, and a move plan stages no diff to print.
    assert "rename" not in ch.HELP_MOVE_QN
    assert "diff" not in ch.HELP_MOVE_DRY_RUN
