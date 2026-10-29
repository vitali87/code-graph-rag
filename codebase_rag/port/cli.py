"""The `cgr port` command group: judge a port between languages (epic #3413)."""

import sys
from pathlib import Path

import typer

from .. import cli_help as ch
from .. import constants as cs
from .. import graph_query
from ..graph_cli import _emit, _project_and_fetch, _unknown_project
from ..project_selection import unknown_project
from . import constants as pcs

cli = typer.Typer(
    help=ch.CMD_PORT_GROUP,
    short_help=ch.CMD_PORT_GROUP,
    epilog=ch.EPILOG_PORT,
    no_args_is_help=True,
    add_completion=False,
)


@cli.command(
    "check",
    help=ch.CMD_PORT_CHECK,
    short_help=ch.CMD_PORT_CHECK,
    epilog=ch.EPILOG_PORT_CHECK,
)
def check_cmd(
    old_scope: str,
    new_scope: str,
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_GRAPH_PROJECT
    ),
    new_project: str | None = typer.Option(
        None, "--new-project", help=ch.HELP_PORT_NEW_PROJECT
    ),
    repo_path: Path = typer.Option(
        Path(cs.MCP_DEFAULT_DIRECTORY),
        "--repo-path",
        exists=True,
        file_okay=False,
        show_default=True,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
    pair_values: list[str] | None = typer.Option(
        None, "--pair", help=ch.HELP_PORT_PAIR
    ),
    include_tests: bool = typer.Option(
        False, "--include-tests", help=ch.HELP_PORT_INCLUDE_TESTS
    ),
    fail_on_found: bool = typer.Option(
        False, "--fail-on-found", help=ch.HELP_PORT_FAIL_ON_FOUND
    ),
) -> None:
    from .parity import (
        ParityReport,
        PortCheckError,
        PortScope,
        has_differences,
        parse_pairs,
        port_check,
    )

    report: ParityReport | None = None
    message: str | None = None
    exit_code = 0
    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:
        refusal = _unknown_project(fetch_all, name, project, repo_path)
        if refusal is not None:
            message, exit_code = refusal.message, refusal.exit_code
        elif new_project is not None and new_project not in (
            indexed := graph_query.indexed_projects(fetch_all)
        ):
            message = unknown_project(new_project, list(indexed))
            exit_code = cs.GRAPH_EXIT_UNKNOWN_PROJECT
        else:
            try:
                report = port_check(
                    fetch_all,
                    PortScope(name, old_scope),
                    PortScope(new_project or name, new_scope),
                    parse_pairs(pair_values or ()),
                    include_tests=include_tests,
                )
            except PortCheckError as error:
                message, exit_code = str(error), error.exit_code
    # Reported after the connection closes, as `cgr graph` does: an exit
    # raised inside it is logged as a failed write.
    if report is None:
        typer.secho(message, fg=cs.Color.RED, err=True)
        sys.exit(exit_code)
    _emit(report)
    if fail_on_found and has_differences(report):
        sys.exit(pcs.PORT_EXIT_DIFFERS)
