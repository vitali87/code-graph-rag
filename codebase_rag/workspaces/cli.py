from __future__ import annotations

import sys

import click
from loguru import logger

from .. import cli_help as ch
from . import constants as wcs
from . import storage as st
from .storage import WorkspaceError


@click.group(
    help=ch.CMD_WORKSPACE_GROUP,
    short_help=ch.CMD_WORKSPACE_GROUP,
    epilog=ch.EPILOG_WORKSPACE,
    no_args_is_help=True,
)
def cli() -> None:
    pass


@cli.command("list", help=ch.CMD_WORKSPACE_LIST, short_help=ch.CMD_WORKSPACE_LIST)
def list_cmd() -> None:
    names = st.list_workspaces()
    if not names:
        click.echo(ch.MSG_NO_WORKSPACES)
        return
    for name in names:
        click.echo(name)


@cli.command("create", help=ch.CMD_WORKSPACE_CREATE, short_help=ch.CMD_WORKSPACE_CREATE)
@click.argument("name")
@click.option("--description", "-d", default="", help=ch.HELP_WORKSPACE_DESCRIPTION)
@click.option("--force", is_flag=True, help=ch.HELP_WORKSPACE_FORCE)
def create_cmd(name: str, description: str, force: bool) -> None:
    try:
        _, path = st.create_workspace(name, description=description, overwrite=force)
    except WorkspaceError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(wcs.MSG_WORKSPACE_CREATED.format(name=name, path=path))


@cli.command("delete", help=ch.CMD_WORKSPACE_DELETE, short_help=ch.CMD_WORKSPACE_DELETE)
@click.argument("name")
def delete_cmd(name: str) -> None:
    try:
        path = st.delete_workspace(name)
    except WorkspaceError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(wcs.MSG_WORKSPACE_DELETED.format(name=name, path=path))


@cli.command("show", help=ch.CMD_WORKSPACE_SHOW, short_help=ch.CMD_WORKSPACE_SHOW)
@click.argument("name")
def show_cmd(name: str) -> None:
    try:
        config = st.load_workspace(name)
    except WorkspaceError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(f"name:        {config.name}")
    if config.description:
        click.echo(f"description: {config.description}")
    click.echo(f"repos:       {len(config.repos)}")
    for repo in config.repos:
        click.echo(f"  - {repo.path} ({repo.project_name})")


@cli.command(
    "add-repo",
    help=ch.CMD_WORKSPACE_ADD_REPO,
    short_help=ch.CMD_WORKSPACE_ADD_REPO,
)
@click.argument("name")
@click.argument("repo_path")
@click.option(
    "--project-name", "-p", default=None, help=ch.HELP_WORKSPACE_REPO_PROJECT_NAME
)
def add_repo_cmd(name: str, repo_path: str, project_name: str | None) -> None:
    try:
        _, repo = st.add_repo(name, repo_path, project_name=project_name)
    except WorkspaceError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(
        wcs.MSG_WORKSPACE_ADDED_REPO.format(
            path=repo.path, project_name=repo.project_name
        )
    )


@cli.command(
    "remove-repo",
    help=ch.CMD_WORKSPACE_REMOVE_REPO,
    short_help=ch.CMD_WORKSPACE_REMOVE_REPO,
)
@click.argument("name")
@click.argument("repo_path")
def remove_repo_cmd(name: str, repo_path: str) -> None:
    try:
        _, repo = st.remove_repo(name, repo_path)
    except WorkspaceError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(wcs.MSG_WORKSPACE_REMOVED_REPO.format(path=repo.path))
