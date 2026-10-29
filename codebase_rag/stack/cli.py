from __future__ import annotations

import sys

import typer

from .. import cli_help as ch
from .manager import StackError, StackManager

cli = typer.Typer(
    help=ch.CMD_DAEMON_GROUP,
    short_help=ch.CMD_DAEMON_GROUP,
    epilog=ch.EPILOG_DAEMON,
    no_args_is_help=True,
    add_completion=False,
)


def _print_status(mgr: StackManager) -> None:
    status = mgr.status()
    typer.echo(f"state:    {status.state.value}")
    typer.echo(
        f"memgraph: {status.memgraph_endpoint} (reachable={status.memgraph_reachable})"
    )
    typer.echo(
        f"qdrant:   {status.qdrant_endpoint} (reachable={status.qdrant_reachable})"
    )
    typer.echo(f"compose:  {status.compose_file}")


@cli.command("up", help=ch.CMD_DAEMON_UP, short_help=ch.CMD_DAEMON_UP)
def up_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.ensure_running()
        _print_status(mgr)
    except StackError as e:
        # Echoed once: logged as well, it printed the whole block twice
        # (issue #2407).
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("down", help=ch.CMD_DAEMON_DOWN, short_help=ch.CMD_DAEMON_DOWN)
def down_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.down()
        typer.echo("stopped")
    except StackError as e:
        # Echoed once: logged as well, it printed the whole block twice
        # (issue #2407).
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("status", help=ch.CMD_DAEMON_STATUS, short_help=ch.CMD_DAEMON_STATUS)
def status_cmd() -> None:
    mgr = StackManager()
    # Status is what a user runs against an already-up stack, the one case the
    # start path's warning never covers (issue #1380).
    mgr.warn_if_ports_are_public()
    _print_status(mgr)


@cli.command("restart", help=ch.CMD_DAEMON_RESTART, short_help=ch.CMD_DAEMON_RESTART)
def restart_cmd() -> None:
    mgr = StackManager()
    try:
        mgr.restart()
        mgr.wait_healthy()
        _print_status(mgr)
    except StackError as e:
        # Echoed once: logged as well, it printed the whole block twice
        # (issue #2407).
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)


@cli.command("logs", help=ch.CMD_DAEMON_LOGS, short_help=ch.CMD_DAEMON_LOGS)
def logs_cmd(
    follow: bool = typer.Option(
        False, "--follow", "-f", help=ch.HELP_DAEMON_LOGS_FOLLOW
    ),
    service: str | None = typer.Option(
        None, "--service", "-s", help=ch.HELP_DAEMON_LOGS_SERVICE
    ),
) -> None:
    mgr = StackManager()
    try:
        rc = mgr.logs(service=service, follow=follow)
        if rc != 0:
            sys.exit(rc)
    except StackError as e:
        # Echoed once: logged as well, it printed the whole block twice
        # (issue #2407).
        typer.secho(str(e), fg="red", err=True)
        sys.exit(1)
