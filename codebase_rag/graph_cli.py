"""The `cgr graph` command group: deterministic graph queries as JSON."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NamedTuple

import typer

from . import cli_help as ch
from . import constants as cs
from . import graph_query
from .project_selection import repo_not_indexed, unknown_project

if TYPE_CHECKING:
    from .services.graph_service import GraphIngestor


def _emit(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT, sort_keys=True))


def _project_and_fetch(
    project: str | None, repo_path: Path
) -> tuple[str, graph_query.QueryFn, GraphIngestor]:
    from .cli_runtime import connect_graph
    from .config import settings
    from .utils.path_utils import derive_project_name

    ingestor = connect_graph(batch_size=settings.resolve_batch_size(None))
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
    # The same refusal every command that reads one project gives (#2415).
    message = (
        repo_not_indexed(repo_path, name, list(roots), roots)
        if project is None
        else unknown_project(name, list(roots))
    )
    return _Refusal(message, cs.GRAPH_EXIT_UNKNOWN_PROJECT)


def _unknown_target(unknown: graph_query.UnknownTargetError) -> _Refusal:
    return _Refusal(
        unknown.refusal(cs.CLI_ERR_GRAPH_UNKNOWN_TARGET, cs.CLI_ERR_GRAPH_RESOLVE_HINT),
        cs.GRAPH_EXIT_UNKNOWN_TARGET,
    )


def _run_query_and_emit(
    project: str | None,
    repo_path: Path,
    query: Callable[[graph_query.QueryFn, str], object],
    target: str | None = None,
) -> None:
    """Run `query` and print its JSON, or refuse on stderr with a status.

    `target` is the qualified name the query walks from; an empty answer
    for it is checked against the graph, because `[]` for a name the graph
    never saw reads as "nothing calls it" (issue #2461). A target the query
    itself refuses (a `path:line` naming no indexed file, or one two files
    share) goes to stderr the same way, not to stdout as JSON a script
    would read as an answer.
    """
    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    result: object = None
    with ingestor:
        refusal = _unknown_project(fetch_all, name, project, repo_path)
        if refusal is None:
            try:
                result = query(fetch_all, name)
                if target is not None and result == []:
                    graph_query.require_known(fetch_all, name, target)
            except graph_query.UnknownTargetError as unknown:
                refusal = _unknown_target(unknown)
    # Reported after the connection closes: an exit raised inside it is
    # logged as a failed write with a traceback.
    if refusal is not None:
        typer.secho(refusal.message, fg=cs.Color.RED, err=True)
        sys.exit(refusal.exit_code)
    _emit(result)


_Project = Annotated[
    str | None, typer.Option("--project", "-p", help=ch.HELP_GRAPH_PROJECT)
]
_RepoPath = Annotated[
    Path,
    typer.Option(
        "--repo-path",
        exists=True,
        file_okay=False,
        show_default=True,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
]
_Depth = Annotated[
    int,
    typer.Option(
        "--depth",
        min=1,
        max=cs.GRAPH_QUERY_MAX_DEPTH,
        show_default=True,
        help=ch.HELP_GRAPH_DEPTH,
    ),
]
_DEFAULT_REPO = Path(cs.MCP_DEFAULT_DIRECTORY)


cli = typer.Typer(
    help=ch.CMD_GRAPH_GROUP,
    short_help=ch.CMD_GRAPH_GROUP,
    epilog=ch.EPILOG_GRAPH,
    no_args_is_help=True,
    add_completion=False,
)


@cli.command("resolve", help=ch.CMD_GRAPH_RESOLVE, short_help=ch.CMD_GRAPH_RESOLVE)
def resolve_cmd(
    target: str, repo_path: _RepoPath = _DEFAULT_REPO, project: _Project = None
) -> None:
    _run_query_and_emit(
        project, repo_path, lambda f, n: graph_query.resolve_or_refuse(f, n, target)
    )


@cli.command(
    "definition", help=ch.CMD_GRAPH_DEFINITION, short_help=ch.CMD_GRAPH_DEFINITION
)
def definition_cmd(
    qualified_name: str,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.definition(
            f, n, qualified_name, graph_query.source_root_for(f, n, repo_path)
        ),
    )


@cli.command("callers", help=ch.CMD_GRAPH_CALLERS, short_help=ch.CMD_GRAPH_CALLERS)
def callers_cmd(
    qualified_name: str,
    depth: _Depth = 1,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.callers(f, n, qualified_name, depth),
        target=qualified_name,
    )


@cli.command("callees", help=ch.CMD_GRAPH_CALLEES, short_help=ch.CMD_GRAPH_CALLEES)
def callees_cmd(
    qualified_name: str,
    depth: _Depth = 1,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
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
def implementors_cmd(
    qualified_name: str,
    depth: _Depth = 1,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
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
def overrides_cmd(
    qualified_name: str,
    depth: _Depth = 1,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
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
def importers_cmd(
    module_qualified_name: str,
    through_reexports: bool = typer.Option(
        False, "--through-reexports", help=ch.HELP_GRAPH_THROUGH_REEXPORTS
    ),
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
) -> None:
    if through_reexports:
        _run_query_and_emit(
            project,
            repo_path,
            lambda f, n: graph_query.importers_through_reexports(
                f, n, module_qualified_name
            ),
            target=module_qualified_name,
        )
        return
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
def tests_reaching_cmd(
    qualified_name: str,
    repo_path: _RepoPath = _DEFAULT_REPO,
    project: _Project = None,
) -> None:
    _run_query_and_emit(
        project,
        repo_path,
        lambda f, n: graph_query.tests_reaching(f, n, qualified_name),
        target=qualified_name,
    )
