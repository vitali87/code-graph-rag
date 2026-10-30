from __future__ import annotations

import sys

import typer
from loguru import logger

from .. import cli_help as ch
from . import constants as wcs
from . import storage as st
from .models import WorkspaceContents
from .storage import WorkspaceError

cli = typer.Typer(
    help=ch.CMD_WORKSPACE_GROUP,
    short_help=ch.CMD_WORKSPACE_GROUP,
    epilog=ch.EPILOG_WORKSPACE,
    no_args_is_help=True,
    add_completion=False,
)


@cli.command("list", help=ch.CMD_WORKSPACE_LIST, short_help=ch.CMD_WORKSPACE_LIST)
def list_cmd() -> None:
    names = st.list_workspaces()
    if not names:
        typer.echo(ch.MSG_NO_WORKSPACES)
        return
    for name in names:
        typer.echo(name)


@cli.command("create", help=ch.CMD_WORKSPACE_CREATE, short_help=ch.CMD_WORKSPACE_CREATE)
def create_cmd(
    name: str,
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        show_default=False,
        help=ch.HELP_WORKSPACE_DESCRIPTION,
    ),
    force: bool = typer.Option(False, "--force", help=ch.HELP_WORKSPACE_FORCE),
) -> None:
    try:
        _, path = st.create_workspace(
            name, contents=WorkspaceContents(description=description), overwrite=force
        )
    except WorkspaceError as e:
        logger.debug(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(wcs.MSG_WORKSPACE_CREATED.format(name=name, path=path))


@cli.command("delete", help=ch.CMD_WORKSPACE_DELETE, short_help=ch.CMD_WORKSPACE_DELETE)
def delete_cmd(name: str) -> None:
    try:
        path = st.delete_workspace(name)
    except WorkspaceError as e:
        logger.debug(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(wcs.MSG_WORKSPACE_DELETED.format(name=name, path=path))


@cli.command("show", help=ch.CMD_WORKSPACE_SHOW, short_help=ch.CMD_WORKSPACE_SHOW)
def show_cmd(name: str) -> None:
    try:
        config = st.load_workspace(name)
    except WorkspaceError as e:
        logger.debug(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(f"name:        {config.name}")
    if config.description:
        typer.echo(f"description: {config.description}")
    typer.echo(f"repos:       {len(config.repos)}")
    for repo in config.repos:
        typer.echo(f"  - {repo.path} ({repo.project_name})")


@cli.command(
    "add-repo",
    help=ch.CMD_WORKSPACE_ADD_REPO,
    short_help=ch.CMD_WORKSPACE_ADD_REPO,
)
def add_repo_cmd(
    name: str,
    repo_path: str,
    project_name: str | None = typer.Option(
        None,
        "--project-name",
        "--project",
        "-p",
        help=ch.HELP_WORKSPACE_REPO_PROJECT_NAME,
    ),
) -> None:
    try:
        _, repo = st.add_repo(name, repo_path, project_name=project_name)
    except WorkspaceError as e:
        logger.debug(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(
        wcs.MSG_WORKSPACE_ADDED_REPO.format(
            path=repo.path, project_name=repo.project_name
        )
    )


@cli.command(
    "remove-repo",
    help=ch.CMD_WORKSPACE_REMOVE_REPO,
    short_help=ch.CMD_WORKSPACE_REMOVE_REPO,
)
def remove_repo_cmd(
    name: str,
    repo_path: str,
) -> None:
    try:
        _, repo = st.remove_repo(name, repo_path)
    except WorkspaceError as e:
        logger.debug(str(e))
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
    typer.echo(wcs.MSG_WORKSPACE_REMOVED_REPO.format(path=repo.path))
