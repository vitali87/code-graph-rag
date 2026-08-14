"""The `cgr trace` command group: runtime call-trace ingestion."""

from __future__ import annotations

import sys
from pathlib import Path

import click
from loguru import logger

from .. import cli_help as ch


@click.group(
    help=ch.CMD_TRACE_GROUP,
    short_help=ch.CMD_TRACE_GROUP,
    epilog=ch.EPILOG_TRACE,
    no_args_is_help=True,
)
def cli() -> None:
    """Group callback: subcommands carry the behaviour."""


@cli.command(
    "ingest",
    help=ch.CMD_TRACE_INGEST,
    short_help=ch.CMD_TRACE_INGEST,
    epilog=ch.EXAMPLES_TRACE_INGEST,
)
@click.argument(
    "trace_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help=ch.HELP_TRACE_REPO_PATH,
)
@click.option("--project-name", default=None, help=ch.HELP_TRACE_PROJECT_NAME)
def ingest_cmd(trace_file: Path, repo_path: Path, project_name: str | None) -> None:
    from ..config import settings
    from ..main import connect_memgraph
    from ..utils.path_utils import derive_project_name
    from .ingest import ingest_trace
    from .records import TraceFormatError

    resolved_project = project_name or derive_project_name(repo_path)
    try:
        with connect_memgraph(batch_size=settings.resolve_batch_size(None)) as ingestor:
            summary = ingest_trace(
                trace_path=trace_file,
                ingestor=ingestor,
                repo_path=repo_path,
                project_name=resolved_project,
            )
    except TraceFormatError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)

    click.echo(
        f"records:          {summary.records}\n"
        f"edges written:    {summary.edges}\n"
        f"confirmed static: {summary.confirmed_static}\n"
        f"static missed:    {summary.static_missed}\n"
        f"unresolved:       {summary.unresolved}"
    )
    for reason, count in sorted(summary.resolution.unresolved.items()):
        click.echo(f"  unresolved[{reason}]: {count}")


@cli.command(
    "convert",
    help=ch.CMD_TRACE_CONVERT,
    short_help=ch.CMD_TRACE_CONVERT,
    epilog=ch.EXAMPLES_TRACE_CONVERT,
)
@click.argument(
    "profile_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--repo-path",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help=ch.HELP_TRACE_REPO_PATH,
)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=ch.HELP_TRACE_OUTPUT,
)
@click.option("--workload", default=None, help=ch.HELP_TRACE_WORKLOAD)
def convert_cmd(
    profile_file: Path,
    repo_path: Path,
    output: Path | None,
    workload: str | None,
) -> None:
    from .. import constants as cs
    from .cpuprofile import convert_cpuprofile
    from .records import TraceFormatError

    resolved_output = output or Path(cs.TRACE_DEFAULT_OUTPUT)
    try:
        count = convert_cpuprofile(
            profile_file,
            repo_root=repo_path,
            output=resolved_output,
            workload=workload,
        )
    except TraceFormatError as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(f"call records written: {count} -> {resolved_output}")
