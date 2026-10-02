"""Issue #2478: `cgr start --update-graph` syncs and exits, so the options only
the assistant reads are refused with it instead of being dropped.

`--update-graph` took its own branch that synced and returned. `-a` was never
asked (exit 0, and `--output-format json` printed a human sync line, not
JSON), `--no-sync` was contradicted by a sync that ran anyway, and `--projects`
was ignored. The help said the command would "continue" after the sync, and
`--project-name` claimed a default (the bare directory name) that is not the
name stored.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from typer.testing import CliRunner

from codebase_rag import cli_help as ch
from codebase_rag import constants as cs
from codebase_rag.cli import app
from codebase_rag.utils.path_utils import derive_project_name

runner = CliRunner()


@dataclass
class _Start:
    sync: MagicMock
    ask: MagicMock
    chat: MagicMock
    stack: MagicMock


@pytest.fixture
def start() -> Generator[_Start, None, None]:
    with (
        patch("codebase_rag.cli._run_graph_sync") as sync,
        patch("codebase_rag.cli.main_single_query") as ask,
        patch("codebase_rag.cli.main_async") as chat,
        patch("codebase_rag.cli.asyncio.run"),
        patch("codebase_rag.cli._maybe_start_stack") as stack,
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        yield _Start(sync=sync, ask=ask, chat=chat, stack=stack)


def _invoke(repo: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["start", "--repo-path", str(repo), *extra])
    return result.exit_code, " ".join(click.unstyle(result.output).split())


@pytest.mark.parametrize(
    ("extra", "option"),
    [
        (["-a", "What does main call?"], "--ask-agent"),
        (["-a", "What does main call?", "--output-format", "json"], "--ask-agent"),
        (["--output-format", "json"], "--output-format json"),
        (["--no-sync"], "--no-sync"),
        (["--projects", "alpha,beta"], "--projects"),
    ],
    ids=["ask-agent", "ask-agent-json", "output-format-json", "no-sync", "projects"],
)
def test_an_option_update_graph_would_drop_is_refused(
    start: _Start, tmp_path: Path, extra: list[str], option: str
) -> None:
    code, output = _invoke(tmp_path, "--update-graph", *extra)

    assert code == 1, output
    assert f"{option} cannot be combined with --update-graph" in output
    # Refused before anything runs: no stack, no sync, no question.
    start.stack.assert_not_called()
    start.sync.assert_not_called()
    start.ask.assert_not_called()


def test_the_ask_agent_refusal_says_start_already_syncs(
    start: _Start, tmp_path: Path
) -> None:
    _, output = _invoke(tmp_path, "--update-graph", "-a", "q")

    assert "Drop --update-graph" in output
    assert "cgr start syncs the graph before" in output


def test_the_no_sync_refusal_names_both_ways_out(start: _Start, tmp_path: Path) -> None:
    _, output = _invoke(tmp_path, "--update-graph", "--no-sync")

    assert "--update-graph to only sync" in output
    assert "--no-sync to start the assistant without syncing" in output


def test_update_graph_help_says_it_exits() -> None:
    assert "continuing" not in ch.HELP_UPDATE_GRAPH
    assert "exit" in ch.HELP_UPDATE_GRAPH
    for option in ("--ask-agent", "--no-sync", "--projects"):
        assert option in ch.HELP_UPDATE_GRAPH


def test_project_name_help_states_the_stored_default(tmp_path: Path) -> None:
    assert "repo directory name" not in ch.HELP_PROJECT_NAME
    assert "cgr status" in ch.HELP_PROJECT_NAME
    # The example has the shape `derive_project_name` really produces.
    [example] = re.findall(r"\b\w+__[0-9a-f]+\b", ch.HELP_PROJECT_NAME)
    derived = derive_project_name(tmp_path / "myrepo")
    assert example.split(cs.PROJECT_NAME_DIGEST_MARKER)[0] == "myrepo"
    assert len(example) == len(derived)


def test_rendered_start_help_shows_the_new_texts() -> None:
    # Wide enough that rich never folds a word of the help column.
    result = runner.invoke(app, ["start", "--help"], env={"COLUMNS": "200"})
    output = " ".join(click.unstyle(result.output).replace("│", " ").split())

    assert result.exit_code == 0
    assert "before continuing" not in output
    assert "repo directory name" not in output
    assert " ".join(ch.HELP_UPDATE_GRAPH.split()) in output


# Negative: what --update-graph honours, and every start without it, is as before.


def test_update_graph_alone_syncs_and_exits(start: _Start, tmp_path: Path) -> None:
    code, output = _invoke(tmp_path, "--update-graph")

    assert code == 0, output
    start.sync.assert_called_once()
    assert start.sync.call_args.kwargs["repo"] == tmp_path.resolve()
    start.ask.assert_not_called()
    start.chat.assert_not_called()


def test_the_options_the_sync_honours_still_reach_it(
    start: _Start, tmp_path: Path
) -> None:
    out = tmp_path / "graph.json"
    code, output = _invoke(
        tmp_path,
        "--update-graph",
        "--clean",
        "--yes",
        "-o",
        str(out),
        "--project-name",
        "tsmove",
        "--exclude",
        "vendor",
        "--capture",
        "calls",
        "--no-embeddings",
        "--batch-size",
        "7",
    )

    assert code == 0, output
    kwargs = start.sync.call_args.kwargs
    assert kwargs["clean"] is True
    assert kwargs["assume_yes"] is True
    assert kwargs["output"] == str(out)
    assert kwargs["project_name"] == "tsmove"
    assert kwargs["project_named"] is True
    assert kwargs["exclude"] == ["vendor"]
    assert kwargs["capture"] == ["calls"]
    assert kwargs["skip_embeddings"] is True
    assert kwargs["batch_size"] == 7


def test_ask_agent_without_update_graph_syncs_then_asks(
    start: _Start, tmp_path: Path
) -> None:
    code, output = _invoke(
        tmp_path, "-a", "q", "--output-format", "json", "--projects", "alpha,beta"
    )

    assert code == 0, output
    start.sync.assert_called_once()
    start.ask.assert_called_once()
    assert start.ask.call_args.kwargs["output_format"] == cs.QueryFormat.JSON
    assert start.ask.call_args.kwargs["active_projects"] == ["alpha", "beta"]


def test_no_sync_without_update_graph_asks_without_syncing(
    start: _Start, tmp_path: Path
) -> None:
    code, output = _invoke(tmp_path, "--no-sync", "-a", "q")

    assert code == 0, output
    start.sync.assert_not_called()
    start.ask.assert_called_once()


def test_output_format_json_without_ask_agent_keeps_its_own_error(
    start: _Start, tmp_path: Path
) -> None:
    code, output = _invoke(tmp_path, "--output-format", "json")

    assert code == 1, output
    assert cs.CLI_ERR_JSON_REQUIRES_ASK_AGENT in output
    assert "--update-graph" not in output


def test_output_without_update_graph_keeps_its_own_error(
    start: _Start, tmp_path: Path
) -> None:
    code, output = _invoke(tmp_path, "-o", str(tmp_path / "graph.json"))

    assert code == 1, output
    assert cs.CLI_ERR_OUTPUT_REQUIRES_UPDATE in output
    start.sync.assert_not_called()
