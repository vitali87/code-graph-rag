"""The `cgr graph` command group: deterministic graph queries as JSON."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import click

from . import cli_help as ch
from . import constants as cs
from . import graph_query

if TYPE_CHECKING:
    from .services.graph_service import MemgraphIngestor


def _emit(payload: object) -> None:
    click.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT, sort_keys=True))


def _project_and_fetch(
    project: str | None, repo_path: Path
) -> tuple[str, graph_query.QueryFn, MemgraphIngestor]:
    from .cli_runtime import connect_memgraph
    from .config import settings
    from .utils.path_utils import derive_project_name

    ingestor = connect_memgraph(batch_size=settings.resolve_batch_size(None))
    name = project or derive_project_name(repo_path.resolve())
    return name, ingestor.fetch_all, ingestor


class _Refusal(NamedTuple):
    message: str
    exit_code: int


def _unknown_project(
    fetch_all: graph_query.QueryFn, name: str, project: str | None, repo_path: Path
) -> _Refusal | None:
    """Why `name` cannot be queried, or None when the graph holds it.

    Checked before any query: every read is scoped by the project prefix,
    so a project the graph does not hold answers `[]` to all of them.
    """
    roots = graph_query.indexed_projects(fetch_all)
    if name in roots:
        return None
    close = graph_query.close_project_names(name, roots)
    if project is None:
        # The name was derived from --repo-path, so the directory is what
        # was never indexed; a project indexed from it under a name of its
        # own is the likeliest thing meant.
        rooted = graph_query.projects_rooted_at(roots, repo_path)
        message = cs.CLI_ERR_GRAPH_REPO_NOT_INDEXED.format(
            path=repo_path.resolve()
        ) + graph_query.did_you_mean(list(dict.fromkeys([*rooted, *close])))
    elif close:
        message = cs.CLI_ERR_GRAPH_UNKNOWN_PROJECT.format(
            project=name
        ) + graph_query.did_you_mean(close)
    elif roots:
        message = cs.CLI_ERR_GRAPH_UNKNOWN_PROJECT.format(
            project=name
        ) + cs.CLI_ERR_GRAPH_INDEXED_PROJECTS.format(
            projects=cs.SEPARATOR_COMMA_SPACE.join(sorted(roots))
        )
    else:
        message = (
            cs.CLI_ERR_GRAPH_UNKNOWN_PROJECT.format(project=name)
            + cs.CLI_ERR_GRAPH_NOTHING_INDEXED
        )
    return _Refusal(message, cs.GRAPH_EXIT_UNKNOWN_PROJECT)


def _unknown_target(
    fetch_all: graph_query.QueryFn, name: str, qualified_name: str
) -> _Refusal | None:
    if graph_query.node_exists(fetch_all, qualified_name):
        return None
    close = graph_query.similar_targets(fetch_all, name, qualified_name)
    message = cs.CLI_ERR_GRAPH_UNKNOWN_TARGET.format(qualified_name=qualified_name)
    message += (
        graph_query.did_you_mean(close) if close else cs.CLI_ERR_GRAPH_RESOLVE_HINT
    )
    return _Refusal(message, cs.GRAPH_EXIT_UNKNOWN_TARGET)


def _run_query_and_emit(
    project: str | None,
    repo_path: Path,
    query: Callable[[graph_query.QueryFn, str], object],
    target: str | None = None,
) -> None:
    """Run `query` and print its JSON, or refuse on stderr with a status.

    `target` is the qualified name the query walks from; an empty answer
    for it is checked against the graph, because `[]` for a name the graph
    never saw reads as "nothing calls it" (issue #2461).
    """
    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:
        refusal = _unknown_project(fetch_all, name, project, repo_path)
        result = None if refusal is not None else query(fetch_all, name)
        if refusal is None and target is not None and result == []:
            refusal = _unknown_target(fetch_all, name, target)
    # Reported after the connection closes: an exit raised inside it is
    # logged as a failed write with a traceback.
    if refusal is not None:
        click.secho(refusal.message, fg=cs.Color.RED, err=True)
        # sys.exit, not click's Exit: the group runs with standalone_mode off
        # under typer, where click turns its own Exit into a return value and
        # the status would be lost.
        sys.exit(refusal.exit_code)
    _emit(result)


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
        target=qualified_name,
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
        target=qualified_name,
    )


@cli.command(
    "implementors", help=ch.CMD_GRAPH_IMPLEMENTORS, short_help=ch.CMD_GRAPH_IMPLEMENTORS
)
@click.argument("qualified_name")
@_depth_option
@_graph_options
def implementors_cmd(
    qualified_name: str, depth: int, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.implementors(f, n, qualified_name, depth),
        target=qualified_name,
    )


@cli.command(
    "overrides", help=ch.CMD_GRAPH_OVERRIDES, short_help=ch.CMD_GRAPH_OVERRIDES
)
@click.argument("qualified_name")
@_depth_option
@_graph_options
def overrides_cmd(
    qualified_name: str, depth: int, project: str | None, repo_path: Path
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.overrides(f, n, qualified_name, depth),
        target=qualified_name,
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
        target=module_qualified_name,
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
        target=qualified_name,
    )
