"""The `cgr edits` command group: show or undo recorded edit transactions."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from .. import cli_help as ch
from .. import constants as cs
from .transaction import TransactionConflict, entry_diff, load_history, undo_steps

if TYPE_CHECKING:
    from ..services.graph_service import GraphIngestor


cli = typer.Typer(
    help=ch.CMD_EDITS_GROUP,
    short_help=ch.CMD_EDITS_GROUP,
    epilog=ch.EPILOG_EDITS,
    no_args_is_help=True,
    add_completion=False,
)

_RepoPath = Annotated[
    Path,
    typer.Option(
        "--repo-path",
        exists=True,
        file_okay=False,
        show_default=True,
        help=ch.HELP_EDITS_REPO_PATH,
    ),
]


@cli.command(
    "show",
    help=ch.CMD_EDITS_SHOW,
    short_help=ch.CMD_EDITS_SHOW,
    epilog=ch.EXAMPLES_EDITS_SHOW,
)
def show_cmd(
    count: int = typer.Option(
        5, "-n", "--count", min=1, show_default=True, help=ch.HELP_EDITS_COUNT
    ),
    show_diff: bool = typer.Option(False, "--diff", help=ch.HELP_EDITS_DIFF),
    repo_path: _RepoPath = Path(cs.MCP_DEFAULT_DIRECTORY),
) -> None:
    entries = load_history(repo_path.resolve())
    if not entries:
        typer.echo(cs.EDIT_SHOW_NONE)
        return
    for entry in reversed(entries[-count:]):
        files = entry.get(cs.EDIT_KEY_FILES, [])
        verification = entry.get(cs.EDIT_KEY_VERIFICATION, {})
        typer.echo(
            cs.EDIT_SHOW_HEADER.format(
                tx=entry.get(cs.EDIT_KEY_ID, ""),
                at=entry.get(cs.EDIT_KEY_AT, ""),
                count=len(files),
                ok=verification.get(cs.EDIT_KEY_OK, True),
            )
        )
        for staged in files:
            typer.echo(f"  {staged.get(cs.KEY_PATH, '')}")
        if show_diff:
            typer.echo(entry_diff(entry))


@cli.command(
    "undo",
    help=ch.CMD_EDITS_UNDO,
    short_help=ch.CMD_EDITS_UNDO,
    epilog=ch.EXAMPLES_EDITS_UNDO,
)
def undo_cmd(
    count: int = typer.Option(
        1, "-n", "--count", min=1, show_default=True, help=ch.HELP_EDITS_COUNT
    ),
    repo_path: _RepoPath = Path(cs.MCP_DEFAULT_DIRECTORY),
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_GRAPH_PROJECT
    ),
) -> None:
    root = repo_path.resolve()
    restored: set[str] = set()
    seen = failed = False
    try:
        for outcome in undo_steps(root, count):
            seen = True
            if outcome.applied:
                restored.update(outcome.files)
                typer.echo(
                    cs.EDIT_UNDO_DONE.format(
                        tx=outcome.transaction_id, count=len(outcome.files)
                    )
                )
            else:
                failed = True
                typer.secho(
                    cs.EDIT_UNDO_STOPPED.format(
                        tx=outcome.transaction_id, reason=outcome.message
                    ),
                    fg="red",
                    err=True,
                )
    except TransactionConflict as error:
        failed = True
        typer.secho(str(error), fg="red", err=True)
    finally:
        # Also when a later step stopped the run: what the earlier steps
        # restored is on disk either way, and the graph has to follow it.
        if restored:
            _resync_graph(root, project, sorted(restored))
    if failed:
        sys.exit(1)
    if not seen:
        typer.echo(cs.EDIT_UNDO_NONE)


def _resync_graph(root: Path, project: str | None, paths: list[str]) -> None:
    """Re-ingest the restored files, as the forward edit re-ingested its own.

    Never raises: the undo has landed, and an error here would read as a
    failed undo and invite a retry that reverses one more transaction. A
    graph that cannot follow is reported with the command that resyncs it.
    """
    from ..cli_runtime import connect_graph
    from ..config import settings
    from ..utils.path_utils import derive_project_name

    name = project or derive_project_name(root)
    result: bool | Exception
    try:
        with connect_graph(batch_size=settings.resolve_batch_size(None)) as ingestor:
            result = _reingest_restored(ingestor, root, name, paths)
    except Exception as error:  # noqa: BLE001 -- returned to the caller, which prints the resync command
        result = error
    if isinstance(result, Exception):
        command = cs.EDIT_UNDO_RESYNC_COMMAND.format(repo=root)
        if project is not None:
            command += cs.EDIT_UNDO_RESYNC_PROJECT.format(project=project)
        typer.secho(
            cs.EDIT_UNDO_GRAPH_STALE.format(error=result, command=command),
            fg="yellow",
            err=True,
        )
    elif result:
        typer.echo(cs.EDIT_UNDO_GRAPH_SYNCED.format(count=len(paths), project=name))
    else:
        typer.echo(cs.EDIT_UNDO_GRAPH_NOT_INDEXED.format(project=name), err=True)


def _reingest_restored(
    ingestor: GraphIngestor, root: Path, name: str, paths: list[str]
) -> bool | Exception:
    """True once re-ingested; False when the project is not in the graph.

    A project the graph does not hold is left out: a scoped re-ingest would
    plant just these files as a partial project that looks indexed. A
    failure is returned, not raised, because leaving the connection's
    context on an exception logs it as a crash, traceback and all.
    """
    from ..config import project_ignore_sets
    from ..graph_updater import GraphUpdater
    from ..parser_loader import load_parsers
    from ..sync.guard import WriteMarker, marked_reingest, sync_owner

    try:
        if name not in ingestor.list_projects():
            return False
        parsers, queries = load_parsers()
        exclude_paths, unignore_paths = project_ignore_sets(root)
        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=root,
            parsers=parsers,
            queries=queries,
            project_name=name,
            exclude_paths=exclude_paths,
            unignore_paths=unignore_paths,
        )
        with sync_owner() as owner:
            marked_reingest(updater, WriteMarker(ingestor, name, owner, None))(paths)
    except Exception as error:  # noqa: BLE001 -- returned to the caller, which prints the resync command
        return error
    return True
