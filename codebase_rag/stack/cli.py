from __future__ import annotations

import sys

import click
from loguru import logger

from .. import cli_help as ch
from .manager import StackError, StackManager


@click.group(
    help=ch.CMD_DAEMON_GROUP,
    short_help=ch.CMD_DAEMON_GROUP,
    epilog=ch.EPILOG_DAEMON,
    no_args_is_help=True,
)
def cli() -> None:
    pass


def _print_status(mgr: StackManager) -> None:
    status = mgr.status()
    click.echo(f"state:    {status.state.value}")
    click.echo(
        f"memgraph: {status.memgraph_endpoint} (reachable={status.memgraph_reachable})"
    )
    click.echo(
        f"qdrant:   {status.qdrant_endpoint} (reachable={status.qdrant_reachable})"
    )
    click.echo(f"compose:  {status.compose_file}")


@cli.command("up", help=ch.CMD_DAEMON_UP, short_help=ch.CMD_DAEMON_UP)
def up_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.ensure_running()
        _print_status(mgr)
    except StackError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("down", help=ch.CMD_DAEMON_DOWN, short_help=ch.CMD_DAEMON_DOWN)
def down_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.down()
        click.echo("stopped")
    except StackError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("status", help=ch.CMD_DAEMON_STATUS, short_help=ch.CMD_DAEMON_STATUS)
def status_cmd() -> None:
    _print_status(StackManager())


@cli.command("restart", help=ch.CMD_DAEMON_RESTART, short_help=ch.CMD_DAEMON_RESTART)
def restart_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.restart()
        mgr.wait_healthy()
        _print_status(mgr)
    except StackError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("logs", help=ch.CMD_DAEMON_LOGS, short_help=ch.CMD_DAEMON_LOGS)
@click.option("--follow", "-f", is_flag=True, help=ch.HELP_DAEMON_LOGS_FOLLOW)
@click.option("--service", "-s", default=None, help=ch.HELP_DAEMON_LOGS_SERVICE)
def logs_cmd(follow: bool, service: str | None) -> None:
    mgr = StackManager()
    try:
        rc = mgr.logs(service=service, follow=follow)
        if rc != 0:
            sys.exit(rc)
    except StackError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
