"""Issue #2704: a command that exits inside `with ingestor:` exits quietly.

`MemgraphIngestor.__exit__` logged every `Exception` with its traceback.
`typer.Exit` is a `RuntimeError` with an empty message, so a command that
ended with a non-zero exit code inside the block (a refused `cgr rename`,
`cgr check` of a project that is not indexed) printed `ERROR ... An
exception occurred: .` and a chained traceback after its own message.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
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

REFUSAL = "Refusing to rename proj.m.Store.get: 1 site was resolved heuristically"

Records = list[tuple[str, str]]


@pytest.fixture
def records() -> Iterator[Records]:
    captured: Records = []
    sink = logger.add(
        lambda message: captured.append(
            (message.record["level"].name, message.record["message"])
        ),
        level="DEBUG",
    )
    try:
        yield captured
    finally:
        logger.remove(sink)


def _levels(records: Records) -> set[str]:
    return {level for level, _message in records}


def _exit(exc: BaseException) -> MagicMock:
    ingestor = MemgraphIngestor(host="localhost", port=7687)
    ingestor.conn = MagicMock()
    with patch.object(MemgraphIngestor, "flush_all") as flush:
        ingestor.__exit__(type(exc), exc, None)
    return flush


@pytest.fixture
def offline_ingestor() -> Iterator[None]:
    # The real `__exit__` runs; only the connection and the writes are stubbed.
    with (
        patch.object(MemgraphIngestor, "__enter__", lambda self: self),
        patch.object(MemgraphIngestor, "flush_all"),
        patch.object(MemgraphIngestor, "list_projects", return_value=[]),
    ):
        yield


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(typer.Exit(code=1), id="typer-exit-1"),
        pytest.param(typer.Exit(code=0), id="typer-exit-0"),
        pytest.param(click.exceptions.Exit(1), id="click-exit"),
        pytest.param(SystemExit(1), id="system-exit"),
    ],
)
def test_a_command_exit_is_no_error_and_no_interrupt(
    records: Records, exc: BaseException
) -> None:
    flush = _exit(exc)

    assert _levels(records) & {"ERROR", "WARNING"} == set()
    flush.assert_called_once_with()


def test_a_refused_rename_prints_the_refusal_and_nothing_else(
    tmp_path: Path, records: Records, offline_ingestor: None
) -> None:
    refused = RenameRefused(REFUSAL, ambiguous=[], unlocatable=["m.py:7"])

    with patch("codebase_rag.editing.rename.rename", side_effect=refused):
        result = CliRunner().invoke(
            app,
            [
                "rename",
                "proj.m.Store.get",
                "fetch",
                "--repo-path",
                str(tmp_path),
                "--dry-run",
            ],
        )

    assert result.exit_code == 1
    assert result.stderr == f"{REFUSAL}\n  m.py:7\n"
    assert result.stdout == ""
    assert "ERROR" not in _levels(records)


def test_check_of_an_unindexed_project_prints_one_line(
    tmp_path: Path, records: Records, offline_ingestor: None
) -> None:
    result = CliRunner().invoke(
        app, ["check", "--repo-path", str(tmp_path), "--project", "proj"]
    )

    assert result.exit_code == 1
    assert result.stderr == cs.CHECK_NOT_INDEXED.format(project="proj") + "\n"
    assert "ERROR" not in _levels(records)


# Negative: what must not change.


def test_an_error_is_still_logged_as_one(records: Records) -> None:
    flush = _exit(ValueError("boom"))

    assert ("ERROR", ls.MG_EXCEPTION.format(error="boom")) in records
    flush.assert_called_once_with()


def test_ctrl_c_is_still_reported_as_an_interrupt(records: Records) -> None:
    flush = _exit(KeyboardInterrupt())

    assert ("WARNING", ls.MG_INTERRUPTED) in records
    assert "ERROR" not in _levels(records)
    flush.assert_called_once_with()


def test_a_failed_flush_after_a_command_exit_is_logged_not_raised(
    records: Records,
) -> None:
    ingestor = MemgraphIngestor(host="localhost", port=7687)
    conn = ingestor.conn = MagicMock()
    exc = typer.Exit(code=1)

    with patch.object(MemgraphIngestor, "flush_all", side_effect=OSError("disk")):
        ingestor.__exit__(type(exc), exc, None)

    assert ("ERROR", ls.MG_FLUSH_ERROR.format(error="disk")) in records
    conn.close.assert_called_once_with()
