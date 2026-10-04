"""Issue #2439: `--capture` says what it does, and a misspelt group is a
usage error rather than a silent default capture.

A bare group name is added to the default groups (structure, calls, types,
imports), so `--capture structure` changed nothing and said nothing; a typo
such as `strcuture` was only a warning, after which the run re-parsed the
whole repository with the defaults and exited 0. The additive meaning stays,
since the docs rely on it (`--capture io` adds I/O to the defaults), but it is
now stated, a group that adds nothing is flagged with how to capture only it,
and an unknown token stops the command before any work.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from loguru import logger
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag.capture import capture_help, default_capture, resolve_capture
from codebase_rag.cli import _capture_selection, app
from codebase_rag.config import settings

runner = CliRunner()

_STRUCTURE = cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.STRUCTURE]
_CALLS = cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.CALLS]
_IO = cs.CAPTURE_GROUP_RELS[cs.CaptureGroup.IO]


def _warnings(tokens: list[str]) -> list[str]:
    messages: list[str] = []
    sink = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        resolve_capture(tokens)
    finally:
        logger.remove(sink)
    return messages


@pytest.fixture
def no_env_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CGR_CAPTURE", "")


@pytest.fixture
def sync() -> Generator[MagicMock, None, None]:
    with (
        patch("codebase_rag.cli._run_graph_sync") as run_graph_sync,
        patch("codebase_rag.cli._update_and_validate_models"),
    ):
        yield run_graph_sync


@pytest.mark.parametrize("token", ["strcuture", "CALLZ", "+nope", "-nope"])
def test_start_refuses_an_unknown_capture_token(
    sync: MagicMock, tmp_path: Path, token: str
) -> None:
    result = runner.invoke(
        app,
        [
            "start",
            "--repo-path",
            str(tmp_path),
            "--update-graph",
            "--no-start-stack",
            "--capture",
            token,
        ],
    )

    output = " ".join(click.unstyle(result.output).split())
    assert result.exit_code == 2, output
    assert token in output
    assert "structure" in output
    assert "io" in output
    sync.assert_not_called()


def test_index_refuses_an_unknown_capture_token(tmp_path: Path) -> None:
    with patch("codebase_rag.cli.load_parsers") as load_parsers:
        result = runner.invoke(
            app,
            [
                "index",
                "--repo-path",
                str(tmp_path),
                "-o",
                str(tmp_path / "out"),
                "--capture",
                "strcuture",
            ],
        )

    assert result.exit_code == 2, result.output
    load_parsers.assert_not_called()


def test_a_group_that_adds_nothing_says_how_to_capture_only_it() -> None:
    warnings = _warnings(["structure"])

    assert any("none" in w and "structure" in w for w in warnings), warnings


def test_a_comma_list_on_the_flag_is_split_like_the_variable(
    no_env_capture: None,
) -> None:
    selection = _capture_selection(["none,structure"])

    assert selection.enabled_rels == _STRUCTURE


def test_the_help_states_that_a_group_is_added_to_the_defaults() -> None:
    # The rendered help: HELP_CAPTURE is a template since #2584.
    text = capture_help()
    assert "on top of the defaults" in text
    assert "none,structure" in text


def test_a_bare_group_still_adds_to_the_defaults(no_env_capture: None) -> None:
    # Negative: `--capture io` is documented as "the defaults plus I/O".
    selection = _capture_selection(["io"])

    assert _IO <= selection.enabled_rels
    assert _CALLS <= selection.enabled_rels


@pytest.mark.parametrize(
    "tokens",
    [["none", "structure"], ["-calls"], ["+calls"], ["io"], ["all"]],
    ids=["none-then-group", "drop", "explicit-add", "new-group", "all"],
)
def test_meaningful_selections_are_not_flagged(tokens: list[str]) -> None:
    # Negative: only a bare group that changes nothing is worth a warning.
    assert _warnings(tokens) == []


def test_none_then_a_group_captures_only_that_group() -> None:
    # Negative.
    assert resolve_capture(["none", "structure"]).enabled_rels == _STRUCTURE


def test_an_unknown_token_in_the_variable_is_still_only_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Negative: `CGR_CAPTURE` is read by long-running servers too, where a
    # typo must not stop them; it keeps its warning.
    monkeypatch.setattr(settings, "CGR_CAPTURE", "strcuture")

    assert default_capture().enabled_rels == resolve_capture([]).enabled_rels
