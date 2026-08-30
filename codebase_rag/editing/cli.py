"""The `cgr edits` command group: show or undo recorded edit transactions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import click

from .. import cli_help as ch
from .. import constants as cs
from .transaction import TransactionConflict, entry_diff, load_history, undo_last


@click.group(
    help=ch.CMD_EDITS_GROUP,
    short_help=ch.CMD_EDITS_GROUP,
    epilog=ch.EPILOG_EDITS,
    no_args_is_help=True,
)
def cli() -> None:
    """Group callback: subcommands carry the behaviour."""


def _repo_option[F: Callable[..., None]](fn: F) -> F:
    return click.option(
        "--repo-path",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=Path(cs.MCP_DEFAULT_DIRECTORY),
        show_default=True,
        help=ch.HELP_EDITS_REPO_PATH,
    )(fn)


@cli.command(
    "show",
    help=ch.CMD_EDITS_SHOW,
    short_help=ch.CMD_EDITS_SHOW,
    epilog=ch.EXAMPLES_EDITS_SHOW,
)
@click.option(
    "-n",
    "--count",
    type=click.IntRange(min=1),
    default=5,
    show_default=True,
    help=ch.HELP_EDITS_COUNT,
)
@click.option(
    "--diff", "show_diff", is_flag=True, default=False, help=ch.HELP_EDITS_DIFF
)
@_repo_option
def show_cmd(count: int, show_diff: bool, repo_path: Path) -> None:
    entries = load_history(repo_path.resolve())
    if not entries:
        click.echo(cs.EDIT_SHOW_NONE)
        return
    for entry in reversed(entries[-count:]):
        files = entry.get(cs.EDIT_KEY_FILES, [])
        verification = entry.get(cs.EDIT_KEY_VERIFICATION, {})
        click.echo(
            cs.EDIT_SHOW_HEADER.format(
                tx=entry.get(cs.EDIT_KEY_ID, ""),
                at=entry.get(cs.EDIT_KEY_AT, ""),
                count=len(files),
                ok=verification.get(cs.EDIT_KEY_OK, True),
            )
        )
        for staged in files:
            click.echo(f"  {staged.get(cs.KEY_PATH, '')}")
        if show_diff:
            click.echo(entry_diff(entry))


@cli.command(
    "undo",
    help=ch.CMD_EDITS_UNDO,
    short_help=ch.CMD_EDITS_UNDO,
    epilog=ch.EXAMPLES_EDITS_UNDO,
)
@click.option(
    "-n",
    "--count",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help=ch.HELP_EDITS_COUNT,
)
@_repo_option
def undo_cmd(count: int, repo_path: Path) -> None:
    try:
        outcomes = undo_last(repo_path.resolve(), count)
    except TransactionConflict as error:
        click.secho(str(error), fg="red", err=True)
        sys.exit(1)
    if not outcomes:
        click.echo(cs.EDIT_UNDO_NONE)
        return
    failed = False
    for outcome in outcomes:
        if outcome.applied:
            click.echo(
                cs.EDIT_UNDO_DONE.format(
                    tx=outcome.transaction_id, count=len(outcome.files)
                )
            )
        else:
            failed = True
            click.secho(
                cs.EDIT_UNDO_STOPPED.format(
                    tx=outcome.transaction_id, reason=outcome.message
                ),
                fg="red",
                err=True,
            )
    if failed:
        sys.exit(1)
