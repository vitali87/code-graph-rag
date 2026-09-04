"""The `cgr graph` command group: deterministic graph queries as JSON."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import click

from . import cli_help as ch
from . import constants as cs
from . import graph_query


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT, sort_keys=True))


def _project_and_fetch(
    project: str | None, repo_path: Path
) -> tuple[str, graph_query.QueryFn, object]:
    from .config import settings
    from .main import connect_memgraph
    from .utils.path_utils import derive_project_name

    ingestor = connect_memgraph(batch_size=settings.resolve_batch_size(None))
    name = project or derive_project_name(repo_path.resolve())
    return name, ingestor.fetch_all, ingestor


def _run_query_and_emit(
    project: str | None,
    repo_path: Path,
    query: Callable[[graph_query.QueryFn, str], object],
) -> None:
    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:  # type: ignore[attr-defined]
        _emit(query(fetch_all, name))


def _graph_options[F: Callable[..., None]](fn: F) -> F:
    fn = click.option("--project", default=None, help=ch.HELP_GRAPH_PROJECT)(fn)
    return click.option(
        "--repo-path",
        type=click.Path(exists=True, file_okay=False, path_type=Path),
        default=Path(cs.MCP_DEFAULT_DIRECTORY),
        show_default=True,
        help=ch.HELP_GRAPH_REPO_PATH,
    )(fn)


@click.group(
    help=ch.CMD_GRAPH_GROUP,
    short_help=ch.CMD_GRAPH_GROUP,
    epilog=ch.EPILOG_GRAPH,
    no_args_is_help=True,
)
def cli() -> None:
    """Group callback: subcommands carry the behaviour."""


@cli.command("resolve", help=ch.CMD_GRAPH_RESOLVE, short_help=ch.CMD_GRAPH_RESOLVE)
@click.argument("target")
@_graph_options
def resolve_cmd(target: str, project: str | None, repo_path: Path) -> None:
    _run_query_and_emit(
        project, repo_path, lambda f, n: graph_query.resolve(f, n, target)
    )


@cli.command(
    "definition", help=ch.CMD_GRAPH_DEFINITION, short_help=ch.CMD_GRAPH_DEFINITION
)
@click.argument("qualified_name")
@_graph_options
def definition_cmd(qualified_name: str, project: str | None, repo_path: Path) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.definition(
            f, n, qualified_name, graph_query.source_root_for(f, n, repo_path)
        ),
    )


def _depth_option[F: Callable[..., None]](fn: F) -> F:
    return click.option(
        "--depth",
        type=click.IntRange(min=1, max=cs.GRAPH_QUERY_MAX_DEPTH),
        default=1,
        show_default=True,
        help=ch.HELP_GRAPH_DEPTH,
    )(fn)


@cli.command("callers", help=ch.CMD_GRAPH_CALLERS, short_help=ch.CMD_GRAPH_CALLERS)
@click.argument("qualified_name")
@_depth_option
@_graph_options
def callers_cmd(
    qualified_name: str, depth: int, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.callers(f, n, qualified_name, depth),
    )


@cli.command("callees", help=ch.CMD_GRAPH_CALLEES, short_help=ch.CMD_GRAPH_CALLEES)
@click.argument("qualified_name")
@_depth_option
@_graph_options
def callees_cmd(
    qualified_name: str, depth: int, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.callees(f, n, qualified_name, depth),
    )


@cli.command(
    "implementors", help=ch.CMD_GRAPH_IMPLEMENTORS, short_help=ch.CMD_GRAPH_IMPLEMENTORS
)
@click.argument("qualified_name")
@_graph_options
def implementors_cmd(qualified_name: str, project: str | None, repo_path: Path) -> None:
    _run_query_and_emit(
        project, repo_path, lambda f, n: graph_query.implementors(f, n, qualified_name)
    )


@cli.command(
    "overrides", help=ch.CMD_GRAPH_OVERRIDES, short_help=ch.CMD_GRAPH_OVERRIDES
)
@click.argument("qualified_name")
@_graph_options
def overrides_cmd(qualified_name: str, project: str | None, repo_path: Path) -> None:
    _run_query_and_emit(
        project, repo_path, lambda f, n: graph_query.overrides(f, n, qualified_name)
    )


@cli.command(
    "importers", help=ch.CMD_GRAPH_IMPORTERS, short_help=ch.CMD_GRAPH_IMPORTERS
)
@click.argument("module_qualified_name")
@_graph_options
def importers_cmd(
    module_qualified_name: str, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.importers(f, n, module_qualified_name),
    )


@cli.command(
    "tests-reaching",
    help=ch.CMD_GRAPH_TESTS_REACHING,
    short_help=ch.CMD_GRAPH_TESTS_REACHING,
)
@click.argument("qualified_name")
@_graph_options
def tests_reaching_cmd(
    qualified_name: str, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.tests_reaching(f, n, qualified_name),
    )
