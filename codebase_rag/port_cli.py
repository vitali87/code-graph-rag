"""The `cgr port` command group: judge a port between languages (epic #3413)."""

import sys
from pathlib import Path

import click

from . import cli_help as ch
from . import constants as cs
from . import graph_query
from .graph_cli import _emit, _project_and_fetch, _unknown_project
from .project_selection import unknown_project


@click.group(
    help=ch.CMD_PORT_GROUP,
    short_help=ch.CMD_PORT_GROUP,
    epilog=ch.EPILOG_PORT,
    no_args_is_help=True,
)
def cli() -> None:
    """Group callback: subcommands carry the behaviour."""


@cli.command(
    "check",
    help=ch.CMD_PORT_CHECK,
    short_help=ch.CMD_PORT_CHECK,
    epilog=ch.EPILOG_PORT_CHECK,
)
@click.argument("old_scope")
@click.argument("new_scope")
@click.option("--project", "-p", default=None, help=ch.HELP_GRAPH_PROJECT)
@click.option("--new-project", default=None, help=ch.HELP_PORT_NEW_PROJECT)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path(cs.MCP_DEFAULT_DIRECTORY),
    show_default=True,
    help=ch.HELP_GRAPH_REPO_PATH,
)
@click.option("--pair", "pair_values", multiple=True, help=ch.HELP_PORT_PAIR)
@click.option("--include-tests", is_flag=True, help=ch.HELP_PORT_INCLUDE_TESTS)
@click.option("--fail-on-found", is_flag=True, help=ch.HELP_PORT_FAIL_ON_FOUND)
def check_cmd(
    old_scope: str,
    new_scope: str,
    project: str | None,
    new_project: str | None,
    repo_path: Path,
    pair_values: tuple[str, ...],
    include_tests: bool,
    fail_on_found: bool,
) -> None:
    from .port_parity import (
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
                    parse_pairs(pair_values),
                    include_tests=include_tests,
                )
            except PortCheckError as error:
                message, exit_code = str(error), error.exit_code
    # Reported after the connection closes, as `cgr graph` does: an exit
    # raised inside it is logged as a failed write.
    if report is None:
        click.secho(message, fg=cs.Color.RED, err=True)
        # sys.exit, not click's Exit: the group runs with standalone_mode off
        # under typer, where click turns its own Exit into a return value.
        sys.exit(exit_code)
    _emit(report)
    if fail_on_found and has_differences(report):
        sys.exit(cs.PORT_EXIT_DIFFERS)
