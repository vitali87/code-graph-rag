"""What a user is told when Ctrl+C stops a sync, and on the run after it (#2442).

Interrupting `cgr start --update-graph` already leaves the right state behind:
the `:IncompleteRun` marker stays down, the hash cache is not published, and
the next sync rebuilds what the interrupted one left. What it said was wrong:

- the interrupted command ended on the ingestor's flush lines, with nothing
  saying the graph was now incomplete or what to run to finish it;
- the next sync blamed "parser code, a grammar or toolchain version" (or a
  wiped database) for a rebuild that only the interrupt caused, because a full
  build's empty placeholder cache with no parser stamp read as a graph some
  older parser had built.
"""

from __future__ import annotations

import json
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import NamedTuple
from unittest.mock import MagicMock, patch

import pytest
import typer
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import exceptions as ex
from codebase_rag import logs as ls
from codebase_rag.cli import _run_graph_sync, _start_update_graph, app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow

runner = CliRunner()

PROJECT = "acme__1a2b3c4d"
STALE_FINGERPRINT = "0" * 32


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from codebase_rag.config import settings

    home = tmp_path / "cgr-home"
    monkeypatch.setattr(settings, "CGR_HOME", home)
    return home


class _CliSync(NamedTuple):
    events: list[str]
    connection: MagicMock
    updater: MagicMock


@pytest.fixture
def cli_sync() -> Generator[_CliSync, None, None]:
    events: list[str] = []
    ingestor = MagicMock()

    def write(query: str, _params: PropertyParams | None = None) -> None:
        if query == cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE:
            events.append("mark")
        elif query == cq.CYPHER_CLEAR_PROJECT_INCOMPLETE:
            events.append("clear")

    ingestor.execute_write.side_effect = write
    connection = MagicMock()
    connection.__enter__.return_value = ingestor
    connection.__exit__.return_value = False
    # `committed` set: a bare MagicMock attribute is truthy, which would read
    # every interrupt as one that landed after the run committed.
    updater = MagicMock(skipped_because_in_sync=False, committed=False)
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
        patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        yield _CliSync(events, connection, updater)


def _invoke_update_graph(repo: Path) -> tuple[int, str]:
    result = runner.invoke(
        app,
        [
            "start",
            "--repo-path",
            str(repo),
            "--project-name",
            PROJECT,
            "--update-graph",
            "--no-start-stack",
            "--no-embeddings",
        ],
    )
    return result.exit_code, result.output


def _lines_calling_the_graph_incomplete(output: str) -> list[str]:
    return [
        line for line in output.splitlines() if PROJECT in line and "incomplete" in line
    ]


def _start_update_graph_directly(repo: Path) -> None:
    _start_update_graph(
        repo,
        PROJECT,
        project_named=True,
        batch_size=10,
        exclude=None,
        interactive_setup=False,
        clean=False,
        output=None,
        capture=None,
        skip_embeddings=True,
        assume_yes=False,
    )


class TestCtrlCDuringUpdateGraph:
    def test_says_in_one_line_that_the_graph_is_incomplete_and_exits_130(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        cli_sync.updater.run.side_effect = KeyboardInterrupt

        exit_code, output = _invoke_update_graph(tmp_path)

        assert exit_code == 130, output
        assert "Traceback" not in output
        assert len(_lines_calling_the_graph_incomplete(output)) == 1, output
        assert cs.CLI_MSG_SYNC_INTERRUPTED.format(project=PROJECT) in output
        assert cs.CLI_MSG_GRAPH_UPDATED not in output
        # The marker that makes `cgr status` say "sync interrupted" stays.
        assert cli_sync.events == ["mark"], cli_sync.events

    def test_ends_with_exit_130_whatever_typer_makes_of_an_interrupt(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        # The typer releases pyproject allows map a stray KeyboardInterrupt to
        # 130 themselves; click's own main makes it "Aborted!" and exit 1. The
        # command sets the status so it does not hinge on that.
        cli_sync.updater.run.side_effect = KeyboardInterrupt

        with pytest.raises((typer.Exit, KeyboardInterrupt)) as raised:
            _start_update_graph_directly(tmp_path)

        assert isinstance(raised.value, typer.Exit), repr(raised.value)
        assert raised.value.exit_code == cs.CLI_EXIT_INTERRUPTED == 130

    def test_the_connection_still_sees_an_interrupt_not_an_error(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        # The ingestor logs an Exception leaving it as a failed write with a
        # traceback, the very output #2442 reported; an interrupt gets one
        # WARNING and the best-effort flush.
        cli_sync.updater.run.side_effect = KeyboardInterrupt

        _invoke_update_graph(tmp_path)

        exc_type = cli_sync.connection.__exit__.call_args.args[0]
        assert issubclass(exc_type, KeyboardInterrupt)
        assert not issubclass(exc_type, Exception)

    def test_the_pre_chat_sync_still_stops_on_a_keyboard_interrupt(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        # `cgr start`'s spinner runs this in a worker that swallows only the
        # KeyboardInterrupt it delivered; anything else would escape as an
        # error.
        cli_sync.updater.run.side_effect = KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _run_graph_sync(
                repo=tmp_path,
                project_name=PROJECT,
                project_named=True,
                batch_size=10,
                exclude=None,
                interactive_setup=False,
            )

        assert cli_sync.events == ["mark"], cli_sync.events


class TestWhatAnInterruptMustNotBeCalled:
    def test_an_interrupt_before_the_marker_does_not_call_the_graph_incomplete(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        # Ctrl+C while connecting: nothing was written, so "incomplete" would
        # send the user to repair a graph that was never touched.
        cli_sync.connection.__enter__.side_effect = KeyboardInterrupt

        exit_code, output = _invoke_update_graph(tmp_path)

        assert exit_code == 130, output
        assert _lines_calling_the_graph_incomplete(output) == [], output
        assert cli_sync.events == [], cli_sync.events

    def test_an_interrupted_embeddings_pass_is_not_called_incomplete(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        # That run committed and recorded the graph before it stopped (#2425).
        cli_sync.updater.run.side_effect = ex.EmbeddingsInterrupted

        exit_code, output = _invoke_update_graph(tmp_path)

        assert exit_code == 130, output
        assert _lines_calling_the_graph_incomplete(output) == [], output
        assert cli_sync.events == ["mark", "clear"], cli_sync.events

    def test_a_real_failure_is_not_reported_as_an_interrupt(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        cli_sync.updater.run.side_effect = RuntimeError("parser crashed")

        result = runner.invoke(
            app,
            [
                "start",
                "--repo-path",
                str(tmp_path),
                "--project-name",
                PROJECT,
                "--update-graph",
                "--no-start-stack",
            ],
        )

        assert isinstance(result.exception, RuntimeError)
        assert result.exit_code == 1
        assert _lines_calling_the_graph_incomplete(result.output) == []

    def test_a_finished_sync_still_reports_completion(
        self, tmp_path: Path, cli_sync: _CliSync
    ) -> None:
        exit_code, output = _invoke_update_graph(tmp_path)

        assert exit_code == 0, output
        assert cs.CLI_MSG_GRAPH_UPDATED in output
        assert _lines_calling_the_graph_incomplete(output) == [], output
        assert cli_sync.events == ["mark", "clear"], cli_sync.events


@pytest.fixture
def py_project(temp_repo: Path) -> Path:
    (temp_repo / "module_a.py").write_text("def func_a():\n    pass\n")
    (temp_repo / "module_b.py").write_text("def func_b():\n    return 1\n")
    return temp_repo


@pytest.fixture
def log_sink() -> Iterator[list[tuple[str, str]]]:
    records: list[tuple[str, str]] = []
    handler_id = logger.add(
        lambda m: records.append((m.record["level"].name, m.record["message"])),
        level="INFO",
        format="{message}",
    )
    yield records
    logger.remove(handler_id)


def _updater(repo: Path, ingestor: MagicMock) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


def _interrupted_run(repo: Path, ingestor: MagicMock) -> None:
    updater = _updater(repo, ingestor)
    # Pass 3, where the issue's Ctrl+C landed: after Pass 2 wrote modules,
    # before the run's commit point.
    with (
        patch.object(
            GraphUpdater, "_process_function_calls", side_effect=KeyboardInterrupt
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        updater.run()


def _graph_holds_no_modules(ingestor: MagicMock) -> None:
    unchanged = ingestor.fetch_all.return_value

    def fetch_all(query: str, params: PropertyParams | None = None) -> list[ResultRow]:
        if query == cs.CYPHER_COUNT_PROJECT_MODULES:
            return [{cs.KEY_COUNT: 0}]
        return unchanged

    ingestor.fetch_all.side_effect = fetch_all


def _warnings(records: list[tuple[str, str]]) -> list[str]:
    return [message for level, message in records if level == "WARNING"]


def _unfinished_notes(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    note = ls.PREVIOUS_SYNC_UNFINISHED.format(project=PROJECT)
    return [(level, message) for level, message in records if message == note]


class TestTheSyncAfterAnInterruptedOne:
    def test_does_not_blame_a_parser_change(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        _interrupted_run(py_project, mock_ingestor)
        log_sink.clear()

        _updater(py_project, mock_ingestor).run()

        warnings = _warnings(log_sink)
        assert ls.PARSER_FINGERPRINT_MISMATCH not in warnings, warnings
        assert _unfinished_notes(log_sink) == [
            ("INFO", ls.PREVIOUS_SYNC_UNFINISHED.format(project=PROJECT))
        ]

    def test_does_not_blame_a_wiped_database(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        # Stopped before any module reached the graph: the orphan check finds
        # none and, without this, says the database was wiped.
        _interrupted_run(py_project, mock_ingestor)
        _graph_holds_no_modules(mock_ingestor)
        log_sink.clear()

        _updater(py_project, mock_ingestor).run()

        orphaned = ls.HASH_CACHE_ORPHANED.format(project=PROJECT)
        warnings = _warnings(log_sink)
        assert orphaned not in warnings, warnings
        assert ls.PARSER_FINGERPRINT_MISMATCH not in warnings, warnings
        assert len(_unfinished_notes(log_sink)) == 1, log_sink

    def test_still_re_indexes_the_whole_repository_and_commits(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _interrupted_run(py_project, mock_ingestor)
        assert not (py_project / cs.PARSER_FINGERPRINT_FILENAME).exists()

        _updater(py_project, mock_ingestor).run()

        hashes = json.loads((py_project / cs.HASH_CACHE_FILENAME).read_text())
        assert set(hashes) == {"module_a.py", "module_b.py"}
        assert (py_project / cs.PARSER_FINGERPRINT_FILENAME).is_file()


class TestWarningsThatMustStay:
    def test_an_interrupted_reindex_after_a_parser_change_still_warns(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        # The graph WAS built by other parser inputs and the interrupted
        # re-index did not replace it, so the warning is still true.
        _updater(py_project, mock_ingestor).run()
        stamp = py_project / cs.PARSER_FINGERPRINT_FILENAME
        stamp.write_text(STALE_FINGERPRINT, encoding="utf-8")
        _interrupted_run(py_project, mock_ingestor)
        log_sink.clear()

        _updater(py_project, mock_ingestor).run()

        assert ls.PARSER_FINGERPRINT_MISMATCH in _warnings(log_sink)
        assert _unfinished_notes(log_sink) == []

    def test_a_graph_built_before_the_stamp_existed_still_warns(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        # Its cache names the files it parsed; only an EMPTY cache is the
        # placeholder of a build that never committed.
        _updater(py_project, mock_ingestor).run()
        (py_project / cs.PARSER_FINGERPRINT_FILENAME).unlink()
        log_sink.clear()

        _updater(py_project, mock_ingestor).run()

        assert ls.PARSER_FINGERPRINT_MISMATCH in _warnings(log_sink)
        assert _unfinished_notes(log_sink) == []

    def test_a_wiped_database_is_still_reported(
        self,
        py_project: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        _updater(py_project, mock_ingestor).run()
        _graph_holds_no_modules(mock_ingestor)
        log_sink.clear()

        _updater(py_project, mock_ingestor).run()

        orphaned = ls.HASH_CACHE_ORPHANED.format(project=PROJECT)
        assert orphaned in _warnings(log_sink)
        assert _unfinished_notes(log_sink) == []

    def test_a_finished_sync_of_a_repository_without_files_is_not_unfinished(
        self,
        temp_repo: Path,
        mock_ingestor: MagicMock,
        log_sink: list[tuple[str, str]],
    ) -> None:
        # It publishes an empty cache too, but stamps the parser that built it.
        _updater(temp_repo, mock_ingestor).run()
        assert json.loads((temp_repo / cs.HASH_CACHE_FILENAME).read_text()) == {}
        log_sink.clear()

        _updater(temp_repo, mock_ingestor).run()

        assert _unfinished_notes(log_sink) == []
        assert ls.PARSER_FINGERPRINT_MISMATCH not in _warnings(log_sink)


@pytest.fixture
def real_sync(mock_ingestor: MagicMock) -> Generator[MagicMock, None, None]:
    # The real updater and parsers behind the CLI; only the database is fake.
    connection = MagicMock()
    connection.__enter__.return_value = mock_ingestor
    connection.__exit__.return_value = False
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=connection),
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        yield mock_ingestor


def _marker_events(ingestor: MagicMock) -> list[str]:
    names = {
        cq.CYPHER_MARK_CLI_SYNC_INCOMPLETE: "mark",
        cq.CYPHER_CLEAR_PROJECT_INCOMPLETE: "clear",
    }
    return [
        names[call.args[0]]
        for call in ingestor.execute_write.call_args_list
        if call.args and call.args[0] in names
    ]


def _recorded_sync(ingestor: MagicMock) -> bool:
    # The sync time `cgr status` lists, stamped on the Project node (#2444).
    return any(
        call.args
        and call.args[0] == cq.CYPHER_RECORD_PROJECT_SYNC
        and call.args[1][cs.KEY_PROJECT_NAME] == PROJECT
        for call in ingestor.execute_write.call_args_list
    )


def _interrupt_right_after_the_commit() -> AbstractContextManager[MagicMock]:
    commit = GraphUpdater._commit_run_state

    def commit_then_interrupt(self: GraphUpdater) -> None:
        commit(self)
        raise KeyboardInterrupt

    return patch.object(
        GraphUpdater,
        "_commit_run_state",
        autospec=True,
        side_effect=commit_then_interrupt,
    )


class TestAnInterruptAroundTheCommit:
    def test_one_right_after_the_commit_finds_the_graph_whole(
        self, py_project: Path, real_sync: MagicMock
    ) -> None:
        # The cache, stamps and graph are saved; only the return is left.
        with _interrupt_right_after_the_commit():
            exit_code, output = _invoke_update_graph(py_project)

        assert exit_code == 130, output
        assert _lines_calling_the_graph_incomplete(output) == [], output
        # Recorded and unmarked like any finished sync, so `cgr status` and
        # the MCP hydration guard do not take a whole graph for a partial one.
        assert _marker_events(real_sync) == ["mark", "clear"]
        assert _recorded_sync(real_sync)
        assert (py_project / cs.PARSER_FINGERPRINT_FILENAME).is_file()

    def test_one_before_the_commit_is_still_called_incomplete(
        self, py_project: Path, real_sync: MagicMock
    ) -> None:
        with patch.object(
            GraphUpdater, "_process_function_calls", side_effect=KeyboardInterrupt
        ):
            exit_code, output = _invoke_update_graph(py_project)

        assert exit_code == 130, output
        assert len(_lines_calling_the_graph_incomplete(output)) == 1, output
        assert _marker_events(real_sync) == ["mark"]
        assert not _recorded_sync(real_sync)

    def test_one_during_the_commit_is_still_called_incomplete(
        self, py_project: Path, real_sync: MagicMock
    ) -> None:
        # Stopped part-way through saving its state, the run cannot vouch for
        # the graph; the next sync re-checks it.
        with patch(
            "codebase_rag.graph_updater._publish_hash_cache",
            side_effect=KeyboardInterrupt,
        ):
            exit_code, output = _invoke_update_graph(py_project)

        assert exit_code == 130, output
        assert len(_lines_calling_the_graph_incomplete(output)) == 1, output
        assert _marker_events(real_sync) == ["mark"]

    def test_a_reused_updater_does_not_keep_a_previous_runs_commit(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        updater = _updater(py_project, mock_ingestor)
        updater.run()
        assert updater.committed is True
        (py_project / "module_a.py").write_text("def func_a():\n    return 2\n")

        with (
            patch.object(
                GraphUpdater, "_process_function_calls", side_effect=KeyboardInterrupt
            ),
            pytest.raises(KeyboardInterrupt),
        ):
            updater.run()

        assert updater.committed is False

    def test_the_in_sync_fast_path_leaves_nothing_partial(
        self, py_project: Path, mock_ingestor: MagicMock
    ) -> None:
        _updater(py_project, mock_ingestor).run()
        updater = _updater(py_project, mock_ingestor)

        updater.run()

        assert updater.skipped_because_in_sync is True
        assert updater.committed is True
