"""A command that ends itself on purpose inside a graph connection (#2414).

`cgr delete-project` with an unknown name, a declined or refused `--clean`,
and a refused `cgr rename` all print their own message and then raise
`typer.Exit` inside `with connect_memgraph(...)`. The connection's `__exit__`
logged every exception as a failed write, so each of those correct, safe
endings was followed by an ERROR line and a traceback that read as a crash.

The ingestor here is the real one; only the socket underneath it is faked,
because mocking the whole context manager is what hid this.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from unittest.mock import MagicMock, patch

import click
import pytest
import typer
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag.cli import app
from codebase_rag.editing.rename import RenameRefused
from codebase_rag.services.graph_service import MemgraphIngestor

if TYPE_CHECKING:
    from loguru import Message

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_STORED_NAME = "click__a52330d6"
_OTHER_PROJECT = "acme"
_THIS_PROJECT = "this-repo"


class _Logged(NamedTuple):
    level: str
    message: str
    error: BaseException | None


@pytest.fixture
def logged() -> Generator[list[_Logged], None, None]:
    records: list[_Logged] = []

    def sink(message: Message) -> None:
        record = message.record
        attached = record["exception"]
        records.append(
            _Logged(
                record["level"].name,
                record["message"],
                attached.value if attached is not None else None,
            )
        )

    sink_id = logger.add(sink, level="DEBUG")
    try:
        yield records
    finally:
        logger.remove(sink_id)


@pytest.fixture
def conn() -> Generator[MagicMock, None, None]:
    connection = MagicMock()
    with patch.object(MemgraphIngestor, "_create_connection", return_value=connection):
        yield connection


def _alarming(records: list[_Logged]) -> list[_Logged]:
    # What a default (INFO) sink would show as a failure: a warning or worse,
    # or any visible line that drags a traceback along.
    return [
        r
        for r in records
        if r.level in {"WARNING", "ERROR", "CRITICAL"}
        or (r.error is not None and r.level != "DEBUG")
    ]


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _ingestor() -> MemgraphIngestor:
    return MemgraphIngestor(host="localhost", port=7687)


_DELIBERATE_EXITS = [
    pytest.param(typer.Exit(), 0, id="typer-exit-0"),
    pytest.param(typer.Exit(1), 1, id="typer-exit-1"),
    pytest.param(typer.Exit(code=3), 3, id="typer-exit-3-keyword"),
    pytest.param(SystemExit(0), 0, id="system-exit-0"),
    pytest.param(SystemExit(4), 4, id="system-exit-4"),
    pytest.param(typer.Abort(), None, id="typer-abort"),
    pytest.param(click.UsageError("no such project"), 2, id="click-usage-error"),
    pytest.param(click.ClickException("refused"), 1, id="click-exception"),
]


def _exit_code(error: BaseException) -> int | str | None:
    match error:
        case SystemExit():
            return error.code
        # typer's own Exit is listed: a typer that vendors click raises a type
        # that is not click's (review of PR 2496).
        case typer.Exit() | click.exceptions.Exit() | click.ClickException():
            return error.exit_code
        case _:
            return None


class TestDeliberateExitInsideTheConnection:
    @pytest.mark.parametrize(("stop", "code"), _DELIBERATE_EXITS)
    def test_is_not_logged_as_a_failure(
        self,
        stop: BaseException,
        code: int | None,
        conn: MagicMock,
        logged: list[_Logged],
    ) -> None:
        with pytest.raises(type(stop)) as raised, _ingestor():
            raise stop

        assert _alarming(logged) == []
        # The exit itself is untouched: same object, so the same exit code.
        assert raised.value is stop
        assert _exit_code(raised.value) == code

    async def test_is_not_logged_as_a_failure_from_async_with(
        self, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        # `cgr start` holds its connection with `async with`.
        stop = typer.Exit(1)
        with pytest.raises(typer.Exit) as raised:
            async with _ingestor():
                raise stop

        assert _alarming(logged) == []
        assert raised.value.exit_code == 1

    def test_keeps_its_cause_for_a_debug_log(
        self, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        # A command may turn a real failure into its own message and an exit
        # (the sync marker write does); the cause must stay reachable with
        # LOGURU_LEVEL=DEBUG rather than vanish.
        cause = ConnectionError("marker write failed")
        with pytest.raises(typer.Exit), _ingestor():
            raise typer.Exit(1) from cause

        debug = [r for r in logged if r.level == "DEBUG" and r.error is not None]
        assert len(debug) == 1
        assert debug[0].error is not None
        assert debug[0].error.__cause__ is cause


class TestWhatMustNotChange:
    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(ConnectionError("bolt connection lost"), id="connection"),
            pytest.param(ValueError("bad row"), id="value"),
            # typer.Exit and click.Abort are RuntimeErrors; the check must not
            # widen to their base and swallow real runtime failures.
            pytest.param(RuntimeError("query failed"), id="runtime"),
            pytest.param(
                RenameRefused("refused", [], []), id="refusal-not-turned-into-exit"
            ),
        ],
    )
    def test_a_real_error_is_still_logged_with_its_traceback_and_propagates(
        self, error: Exception, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        with pytest.raises(type(error)) as raised, _ingestor():
            raise error

        assert raised.value is error
        errors = [r for r in logged if r.level == "ERROR"]
        assert [r.message for r in errors] == [ls.MG_EXCEPTION.format(error=error)]
        assert errors[0].error is error

    @pytest.mark.parametrize(
        "stop",
        [
            pytest.param(None, id="normal-exit"),
            pytest.param(typer.Exit(1), id="deliberate-exit"),
            pytest.param(SystemExit(2), id="system-exit"),
            pytest.param(RuntimeError("query failed"), id="real-error"),
        ],
    )
    def test_the_connection_is_flushed_and_closed_on_every_way_out(
        self, stop: BaseException | None, conn: MagicMock
    ) -> None:
        ingestor = _ingestor()
        with patch.object(MemgraphIngestor, "flush_all") as flush:
            if stop is None:
                with ingestor:
                    pass
            else:
                with pytest.raises(type(stop)), ingestor:
                    raise stop

        # A deliberate exit ends the command like a return does, so what it
        # buffered is still written.
        flush.assert_called_once_with()
        conn.close.assert_called_once_with()
        assert ingestor._executor is None

    def test_a_flush_failure_on_a_deliberate_exit_is_reported_and_keeps_the_exit(
        self, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        lost = RuntimeError("write lost")
        with (
            patch.object(MemgraphIngestor, "flush_all", side_effect=lost),
            pytest.raises(typer.Exit) as raised,
            _ingestor(),
        ):
            raise typer.Exit(3)

        assert raised.value.exit_code == 3
        assert (
            "ERROR",
            ls.MG_FLUSH_ERROR.format(error=lost),
        ) in {(r.level, r.message) for r in logged}
        conn.close.assert_called_once_with()


def _with_projects(*names: str) -> AbstractContextManager[MagicMock]:
    return patch.object(MemgraphIngestor, "list_projects", return_value=list(names))


class TestDeletingAProjectThatIsNotThere:
    def test_prints_only_the_not_found_message(
        self, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        with (
            _with_projects(_STORED_NAME, _OTHER_PROJECT),
            patch("codebase_rag.cli.delete_project_embeddings") as embeddings,
        ):
            result = runner.invoke(app, ["delete-project", "-n", "click"])

        assert result.exit_code == 1, result.output
        assert "Project 'click' not found" in _plain(result.output)
        assert _alarming(logged) == []
        embeddings.assert_not_called()
        conn.close.assert_called_once_with()

    def test_a_graph_failure_is_still_reported_as_one(
        self, conn: MagicMock, logged: list[_Logged]
    ) -> None:
        failure = ConnectionError("bolt connection lost")
        with patch.object(MemgraphIngestor, "list_projects", side_effect=failure):
            result = runner.invoke(app, ["delete-project", "-n", "click"])

        assert result.exit_code == 1, result.output
        assert "bolt connection lost" in _plain(result.output)
        assert ls.MG_EXCEPTION.format(error=failure) in {
            r.message for r in logged if r.level == "ERROR"
        }
        conn.close.assert_called_once_with()


def _clean_args(repo: Path) -> list[str]:
    return [
        "start",
        "--clean",
        "--repo-path",
        str(repo),
        "--project-name",
        _THIS_PROJECT,
    ]


class TestCleanThatDoesNotGoAhead:
    @pytest.fixture(autouse=True)
    def wipe(self, conn: MagicMock) -> Generator[MagicMock, None, None]:
        with (
            _with_projects(_OTHER_PROJECT, _THIS_PROJECT),
            patch.object(MemgraphIngestor, "clean_database") as wipe,
        ):
            yield wipe

    def test_declining_the_prompt_prints_only_the_abort_message(
        self,
        wipe: MagicMock,
        conn: MagicMock,
        logged: list[_Logged],
        tmp_path: Path,
    ) -> None:
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
            result = runner.invoke(app, _clean_args(tmp_path), input="n\n")

        assert result.exit_code == 1, result.output
        assert cs.CLI_MSG_CLEAN_ABORTED in _plain(result.output)
        assert _alarming(logged) == []
        wipe.assert_not_called()
        conn.close.assert_called_once_with()

    def test_closing_the_prompt_prints_only_the_abort_message(
        self,
        wipe: MagicMock,
        conn: MagicMock,
        logged: list[_Logged],
        tmp_path: Path,
    ) -> None:
        # Ctrl+D (or Ctrl+C) at the prompt makes click raise Abort, which is
        # the user saying no as surely as typing it.
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=True):
            result = runner.invoke(app, _clean_args(tmp_path), input="")

        assert result.exit_code == 1, result.output
        assert "Aborted" in _plain(result.output)
        assert _alarming(logged) == []
        wipe.assert_not_called()

    def test_refusing_without_a_terminal_prints_only_the_refusal(
        self,
        wipe: MagicMock,
        conn: MagicMock,
        logged: list[_Logged],
        tmp_path: Path,
    ) -> None:
        with patch("codebase_rag.cli._stdin_is_interactive", return_value=False):
            result = runner.invoke(app, _clean_args(tmp_path))

        assert result.exit_code == 1, result.output
        assert "Refusing to run --clean without a terminal" in _plain(result.output)
        assert _alarming(logged) == []
        wipe.assert_not_called()
        conn.close.assert_called_once_with()


class TestRenameRefusal:
    def test_prints_only_the_refusal(
        self, conn: MagicMock, logged: list[_Logged], tmp_path: Path
    ) -> None:
        refusal = RenameRefused("Refusing to rename proj.ov.fmt@3", [], [])
        with (
            patch("codebase_rag.cli.load_parsers", return_value=({}, {})),
            patch("codebase_rag.graph_updater.GraphUpdater"),
            patch("codebase_rag.editing.rename.rename", side_effect=refusal),
        ):
            result = runner.invoke(
                app,
                [
                    "rename",
                    "proj.ov.fmt@3",
                    "format",
                    "--repo-path",
                    str(tmp_path),
                    "--project",
                    "proj",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 1, result.output
        assert "Refusing to rename proj.ov.fmt@3" in _plain(result.output)
        assert _alarming(logged) == []
        conn.close.assert_called_once_with()
