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


class _ConvertUsageError(Exception):
    """A convert invocation missing an option the detected format requires."""


def _convert_for_language(
    profile_file: Path,
    repo_path: Path | None,
    resolved_output: Path,
    workload: str | None,
    language: str,
) -> int:
    """Convert with an explicit ``--language`` override, bypassing sniffing.

    Rust pprof profiles are gzipped protobufs indistinguishable from Go's by
    magic bytes, so an explicit override is the only way to select the Rust
    demangler.
    """
    from .. import constants as cs

    if language == cs.TRACE_LANGUAGE_RUST:
        from .rust_pprof import convert_rust_pprof

        return convert_rust_pprof(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )
    raise _ConvertUsageError(
        ch.ERR_TRACE_CONVERT_BAD_LANGUAGE.format(
            language=language, supported=cs.TRACE_LANGUAGE_RUST
        )
    )


def _require_repo(repo_path: Path | None) -> Path:
    """Return ``repo_path`` or raise the usage error when a format needs it."""
    if repo_path is None:
        raise _ConvertUsageError(ch.ERR_TRACE_CONVERT_NEEDS_REPO)
    return repo_path


def _convert_profile(
    profile_file: Path,
    repo_path: Path | None,
    resolved_output: Path,
    include: str | None,
    workload: str | None,
    language: str | None = None,
) -> int:
    """Dispatch by profile format and write records; returns the record count.

    Raises ``_ConvertUsageError`` when the format needs an option the caller
    did not supply, and ``TraceFormatError``/``OSError``/``JSONDecodeError``
    for unreadable or malformed profiles.
    """
    import json

    if language is not None:
        return _convert_for_language(
            profile_file, repo_path, resolved_output, workload, language
        )

    if profile_file.suffix == ".addrs":
        # C/C++ instrumented address traces need symbolisation plus the repo.
        from .instrumented import convert_instrumented

        return convert_instrumented(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    with profile_file.open("rb") as fh:
        magic = fh.read(2)

    if magic == b"\x1f\x8b" or profile_file.suffix == ".pprof":
        # Go pprof CPU profiles are gzipped protobufs.
        from .pprof import convert_pprof

        return convert_pprof(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    if profile_file.suffix == ".xt":
        from .xdebug import convert_xdebug_trace

        return convert_xdebug_trace(
            profile_file, output=resolved_output, workload=workload
        )

    raw = json.loads(profile_file.read_text(encoding="utf-8"))
    looks_like_cpuprofile = (isinstance(raw, dict) and "nodes" in raw) or (
        profile_file.suffix == ".cpuprofile"
    )
    if looks_like_cpuprofile:
        # V8 cpuprofile: frames carry file URLs, so scoping needs the repo.
        from .cpuprofile import convert_cpuprofile

        return convert_cpuprofile(
            profile_file,
            repo_root=_require_repo(repo_path),
            output=resolved_output,
            workload=workload,
        )

    # dotnet-trace speedscope: frames carry no paths, so scoping needs
    # namespace prefixes.
    from .speedscope import convert_speedscope

    prefixes = tuple(p.strip() for p in (include or "").split(",") if p.strip())
    if not prefixes:
        raise _ConvertUsageError(ch.ERR_TRACE_CONVERT_NEEDS_INCLUDE)
    return convert_speedscope(
        profile_file, output=resolved_output, include=prefixes, workload=workload
    )


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
    default=None,
    help=ch.HELP_TRACE_REPO_PATH,
)
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help=ch.HELP_TRACE_OUTPUT,
)
@click.option("--include", default=None, help=ch.HELP_TRACE_INCLUDE)
@click.option("--workload", default=None, help=ch.HELP_TRACE_WORKLOAD)
@click.option("--language", default=None, help=ch.HELP_TRACE_LANGUAGE)
def convert_cmd(
    profile_file: Path,
    repo_path: Path | None,
    output: Path | None,
    include: str | None,
    workload: str | None,
    language: str | None,
) -> None:
    from .. import constants as cs

    resolved_output = output or Path(cs.TRACE_DEFAULT_OUTPUT)
    try:
        count = _convert_profile(
            profile_file, repo_path, resolved_output, include, workload, language
        )
    # TraceFormatError subclasses ValueError, so ValueError covers it (and the
    # malformed-number / non-UTF-8 cases) without listing it redundantly.
    except (OSError, ValueError, _ConvertUsageError) as e:
        logger.error(str(e))
        click.secho(str(e), fg="red", err=True)
        sys.exit(1)
    click.echo(f"call records written: {count} -> {resolved_output}")
