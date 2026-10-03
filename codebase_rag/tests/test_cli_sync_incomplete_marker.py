"""The CLI sync carries the `:IncompleteRun` marker the MCP paths do (#2219).

`_run_graph_sync` committed graph writes with nothing durable saying the run
had not finished: an interrupted `cgr start --update-graph` left the graph
ahead of `last_sync`, and a later MCP process hydrated from it as if whole.
"""

from __future__ import annotations

import os
import re
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import typer
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import _project_syncs, _run_graph_sync, app
from codebase_rag.stack.constants import StackState
from codebase_rag.stack.manager import StackStatus
from codebase_rag.types_defs import ProjectSync
from codebase_rag.utils.process_owner import process_host

runner = CliRunner()


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    return home


@pytest.fixture
def events() -> list[str]:
    return []


@pytest.fixture
def ingestor(events: list[str]) -> MagicMock:
    store = MagicMock()

    def write(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE:
            events.append("mark")
        elif query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            events.append("clear")

    store.execute_write.side_effect = write
    store.clean_database.side_effect = lambda: events.append("clean")
    store.ensure_constraints.side_effect = lambda: events.append("constraints")
    return store


@pytest.fixture
def updater(events: list[str]) -> MagicMock:
    instance = MagicMock()
    instance.run.side_effect = lambda: events.append("run")
    instance.skipped_because_in_sync = False
    return instance


@pytest.fixture
def sync_env(
    ingestor: MagicMock, updater: MagicMock, events: list[str]
) -> Generator[MagicMock, None, None]:
    connection = MagicMock()
    connection.__enter__.return_value = ingestor
    connection.__exit__.return_value = False

    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater) as cls,
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli.clear_all_embeddings"),
        patch("codebase_rag.cli._confirm_destructive_clean"),
    ):
        yield cls


def _sync(repo: Path, *, clean: bool = False) -> None:
    _run_graph_sync(
        repo=repo,
        project_name="proj",
        project_named=True,
        batch_size=10,
        exclude=None,
        interactive_setup=False,
        clean=clean,
    )


def _marker_writes(ingestor: MagicMock, query: str) -> list[dict]:
    return [
        call.args[1]
        for call in ingestor.execute_write.call_args_list
        if call.args[0] == query
    ]


def test_a_sync_marks_before_writing_and_clears_after_running(
    tmp_path: Path, sync_env: MagicMock, ingestor: MagicMock, events: list[str]
) -> None:
    _sync(tmp_path)

    assert events == ["mark", "constraints", "run", "clear"], events
    (mark,) = _marker_writes(ingestor, cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE)
    token = mark.pop(cs.KEY_OWNER_TOKEN)
    assert isinstance(token, str)
    assert token
    # The owner, so a delete can tell whether this sync still runs (#2532).
    assert mark == {
        cs.KEY_PROJECT_NAME: "proj",
        cs.KEY_RUN_ID: cs.CLI_SYNC_RUN_ID,
        cs.KEY_WRITING: True,
        cs.KEY_OWNER_HOST: process_host(),
        cs.KEY_OWNER_PID: os.getpid(),
    }
    (clear,) = _marker_writes(ingestor, cq.CYPHER_CLEAR_PROJECT_INCOMPLETE)
    assert clear == {cs.KEY_PROJECT_NAME: "proj", cs.KEY_RUN_ID: cs.CLI_SYNC_RUN_ID}


def test_a_failed_sync_keeps_its_marker(
    tmp_path: Path, sync_env: MagicMock, updater: MagicMock, events: list[str]
) -> None:
    def interrupted() -> None:
        events.append("run")
        raise KeyboardInterrupt

    updater.run.side_effect = interrupted
    with pytest.raises(KeyboardInterrupt):
        _sync(tmp_path)

    assert events == ["mark", "constraints", "run"], events


def test_a_clean_sync_marks_after_the_wipe(
    tmp_path: Path, sync_env: MagicMock, events: list[str]
) -> None:
    # The wipe deletes every node, the marker included, so a marker written
    # before it would not survive to guard the rebuild.
    _sync(tmp_path, clean=True)

    assert events[:3] == ["clean", "mark", "constraints"], events


def test_a_marker_that_cannot_be_written_aborts_before_any_change(
    tmp_path: Path,
    sync_env: MagicMock,
    ingestor: MagicMock,
    updater: MagicMock,
    events: list[str],
) -> None:
    ingestor.execute_write.side_effect = RuntimeError("store refused the write")

    with pytest.raises(typer.Exit) as raised:
        _sync(tmp_path)

    assert raised.value.exit_code == 1
    assert events == [], events
    updater.run.assert_not_called()


def test_a_marker_that_cannot_be_cleared_does_not_fail_the_sync(
    tmp_path: Path, sync_env: MagicMock, ingestor: MagicMock, events: list[str]
) -> None:
    def write(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            raise RuntimeError("store went away")
        events.append("mark")

    ingestor.execute_write.side_effect = write

    _sync(tmp_path)

    assert events == ["mark", "constraints", "run"], events


def _status(reachable: bool) -> StackStatus:
    return StackStatus(
        state=StackState.RUNNING if reachable else StackState.STOPPED,
        memgraph_reachable=reachable,
        qdrant_reachable=reachable,
        compose_file=Path("/tmp/cgr/docker-compose.yaml"),
        memgraph_endpoint="localhost:7687",
        qdrant_endpoint="localhost:6333",
    )


def _graph(
    projects: dict[str, str | None], incomplete: set[str]
) -> tuple[MagicMock, MagicMock]:
    store = MagicMock()

    def fetch_all(query: str, params: dict | None = None) -> list[dict]:
        if query == cq.CYPHER_PROJECT_SYNC_TIMES:
            return [
                {cs.KEY_NAME: name, cs.KEY_LAST_SYNCED_AT: synced_at}
                for name, synced_at in sorted(projects.items())
            ]
        if query == cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS:
            return [{"project": project} for project in sorted(incomplete)]
        raise AssertionError(query)

    store.fetch_all.side_effect = fetch_all
    connection = MagicMock()
    connection.__enter__.return_value = store
    connection.__exit__.return_value = False
    return connection, store


def _invoke_status(
    reachable: bool, projects: dict[str, str | None], incomplete: set[str]
) -> tuple[str, MagicMock]:
    connection, _ = _graph(projects, incomplete)
    with (
        patch("codebase_rag.cli.StackManager") as manager,
        patch("codebase_rag.cli.connect_memgraph", return_value=connection) as connect,
    ):
        manager.return_value.status.return_value = _status(reachable)
        result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    # Rich colours and wraps the lines; compare the words alone.
    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    return " ".join(plain.split()), connect


def test_status_flags_a_project_whose_sync_did_not_finish() -> None:
    synced = {"alpha": "2026-09-29T10:00:00+00:00", "beta": "2026-09-29T11:00:00+00:00"}

    output, _ = _invoke_status(True, synced, {"alpha"})

    alpha, beta = output.split("- alpha:")[1].split("- beta:")
    assert cs.CLI_STATUS_SYNC_INCOMPLETE in alpha, output
    assert cs.CLI_STATUS_SYNC_INCOMPLETE not in beta, output


def test_status_lists_an_interrupted_first_sync() -> None:
    # A first sync that never finished may not have its Project node yet;
    # the marker is the only trace of it.
    output, _ = _invoke_status(True, {}, {"fresh"})

    assert "no projects" not in output
    assert "fresh" in output
    assert cs.CLI_STATUS_SYNC_INCOMPLETE in output


def test_status_does_not_query_an_unreachable_graph() -> None:
    output, connect = _invoke_status(False, {"alpha": None}, {"alpha"})

    connect.assert_not_called()
    assert "alpha" not in output


def test_reading_the_graph_is_best_effort() -> None:
    connection = MagicMock()
    connection.__enter__.side_effect = ConnectionError("memgraph is down")
    with patch("codebase_rag.cli.connect_memgraph", return_value=connection):
        assert _project_syncs() is None


def test_reading_the_graph_joins_projects_and_markers() -> None:
    connection, store = _graph(
        {"a": "2026-09-29T10:00:00+00:00", "b": None}, {"b", "c"}
    )
    with patch("codebase_rag.cli.connect_memgraph", return_value=connection):
        syncs = _project_syncs()

    assert syncs == [
        ProjectSync("a", "2026-09-29T10:00:00+00:00", interrupted=False),
        ProjectSync("b", None, interrupted=True),
        ProjectSync("c", None, interrupted=True),
    ]
    queried = [call.args[0] for call in store.fetch_all.call_args_list]
    assert queried == [
        cq.CYPHER_PROJECT_SYNC_TIMES,
        cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS,
    ]
