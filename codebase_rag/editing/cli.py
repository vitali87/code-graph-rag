"""The `cgr edits` command group: show or undo recorded edit transactions."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import click

from .. import cli_help as ch
from .. import constants as cs
from .transaction import TransactionConflict, entry_diff, load_history, undo_steps

if TYPE_CHECKING:
    from ..services.graph_service import MemgraphIngestor


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
@click.option("--project", default=None, help=ch.HELP_GRAPH_PROJECT)
def undo_cmd(count: int, repo_path: Path, project: str | None) -> None:
    root = repo_path.resolve()
    restored: set[str] = set()
    seen = failed = False
    try:
        for outcome in undo_steps(root, count):
            seen = True
            if outcome.applied:
                restored.update(outcome.files)
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
    except TransactionConflict as error:
        failed = True
        click.secho(str(error), fg="red", err=True)
    finally:
        # Also when a later step stopped the run: what the earlier steps
        # restored is on disk either way, and the graph has to follow it.
        if restored:
            _resync_graph(root, project, sorted(restored))
    if failed:
        sys.exit(1)
    if not seen:
        click.echo(cs.EDIT_UNDO_NONE)


def _resync_graph(root: Path, project: str | None, paths: list[str]) -> None:
    """Re-ingest the restored files, as the forward edit re-ingested its own.

    Never raises: the undo has landed, and an error here would read as a
    failed undo and invite a retry that reverses one more transaction. A
    graph that cannot follow is reported with the command that resyncs it.
    """
    from ..cli_runtime import connect_memgraph
    from ..config import settings
    from ..utils.path_utils import derive_project_name

    name = project or derive_project_name(root)
    result: bool | Exception
    try:
        with connect_memgraph(batch_size=settings.resolve_batch_size(None)) as ingestor:
            result = _reingest_restored(ingestor, root, name, paths)
    except Exception as error:
        result = error
    if isinstance(result, Exception):
        command = cs.EDIT_UNDO_RESYNC_COMMAND.format(repo=root)
        if project is not None:
            command += cs.EDIT_UNDO_RESYNC_PROJECT.format(project=project)
        click.secho(
            cs.EDIT_UNDO_GRAPH_STALE.format(error=result, command=command),
            fg="yellow",
            err=True,
        )
    elif result:
        click.echo(cs.EDIT_UNDO_GRAPH_SYNCED.format(count=len(paths), project=name))
    else:
        click.echo(cs.EDIT_UNDO_GRAPH_NOT_INDEXED.format(project=name), err=True)


def _reingest_restored(
    ingestor: MemgraphIngestor, root: Path, name: str, paths: list[str]
) -> bool | Exception:
    """True once re-ingested; False when the project is not in the graph.

    A project the graph does not hold is left out: a scoped re-ingest would
    plant just these files as a partial project that looks indexed. A
    failure is returned, not raised, because leaving the connection's
    context on an exception logs it as a crash, traceback and all.
    """
    from ..graph_updater import GraphUpdater
    from ..parser_loader import load_parsers

    try:
        if name not in ingestor.list_projects():
            return False
        parsers, queries = load_parsers()
        GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=name,
        ).reingest(paths)
    except Exception as error:
        return error
    return True
