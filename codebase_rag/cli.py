"""Command-line entry point wiring cgr subcommands to their handlers."""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Coroutine, Generator, Iterable
from contextlib import ExitStack, contextmanager
from fnmatch import fnmatch
from functools import partial, wraps
from importlib.metadata import version as get_version
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

import click
import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from typer.core import TyperGroup

if TYPE_CHECKING:
    # What `TyperGroup.invoke` takes on the locked typer, which vendors click;
    # an older typer passes click's own Context at runtime (#1409).
    from typer._click import Context as _GroupContext

from . import _cli_env  # noqa: F401  (must run before settings load)
from . import cli_help as ch
from . import constants as cs
from . import cypher_queries as cq
from . import exceptions as ex
from . import logs as ls
from .capture import (
    CaptureSelection,
    capture_help,
    resolve_capture,
    split_spec,
    unknown_tokens,
)
from .checkout_state import prepare_state_dir
from .cli_runtime import app_context, connect_memgraph, dim, style
from .config import load_ignore_patterns, settings, settings_errors
from .console_marks import status_mark
from .editing.cli import cli as edits_cli
from .editor_links import (
    EditorTemplateError,
    diff_command,
    diff_link,
    editor_url,
    resolve_editor,
    url_template_problem,
)
from .graph_cli import cli as graph_cli
from .graph_dialects import DIALECT_MEMGRAPH
from .parser_loader import load_parsers
from .port_cli import cli as port_cli
from .project_selection import ProjectChoice, choose_project, scope_refusal
from .services.gloss_cleanup import prune_orphaned_glosses
from .services.graph_diff import DiffError, diff_indexes, diff_is_empty
from .services.graph_service import GraphUnavailableError, MemgraphIngestor
from .services.protobuf_service import ProtobufFileIngestor
from .services.provenance import (
    capture_description,
    source_state,
    verify_index,
    write_manifest,
)
from .stack import StackManager
from .stack.cli import cli as daemon_cli
from .stack.constants import SERVICE_MEMGRAPH, SERVICE_QDRANT, StackState
from .stack.manager import StackError
from .sync_guard import RUNNING_SYNC_TOKENS as _RUNNING_SYNC_TOKENS
from .sync_guard import OwnerCheck as _OwnerCheck
from .sync_guard import SyncRefusedError, mark_sync_incomplete, release_project_claim
from .sync_guard import clear_sync_incomplete as _clear_sync_incomplete
from .sync_guard import partial_graph_refusal as _partial_graph_refusal
from .sync_guard import project_owner_refusal as _project_owner_refusal
from .sync_guard import retire_stranded_runs as _retire_stranded_runs
from .sync_guard import stranded_runs as _stranded_runs
from .sync_guard import sync_marker_params as _sync_marker_params
from .sync_guard import sync_owner as _sync_owner
from .sync_lock import SyncLockError, repo_sync_lock
from .tools.health_checker import HealthChecker
from .tools.language import cli as language_cli
from .trace.cli import cli as trace_cli
from .types_defs import (
    DeadCodeConfig,
    DeadCodeRow,
    DuplicateGroup,
    DuplicateMember,
    DuplicatesConfig,
    DuplicatesReport,
    ProjectSync,
    PropertyValue,
    ReingestReport,
    ResultRow,
    ResultValue,
)
from .utils.path_utils import (
    derive_project_name,
    nested_project_names,
    normalize_project_name,
    project_name_error,
    project_roots_from_rows,
    projects_sharing_root,
    resolve_repo_path,
    unwritable_output_reason,
)
from .utils.process_owner import pid_may_be_running, process_host
from .utils.terminal_console import terminal_aware_console
from .workspaces import WorkspaceConfig, WorkspaceError, load_workspace
from .workspaces.cli import cli as workspace_cli

if TYPE_CHECKING:
    from .graph_updater import GraphUpdater
    from .watch import WriteMarker


def _vendored_click_exception() -> type[click.ClickException]:
    # A typer that vendors click raises the vendored exceptions, which do not
    # descend from the real click's; both flavors must be caught (#1409).
    try:
        vendored = importlib.import_module(cs.TYPER_VENDORED_CLICK_EXCEPTIONS_MODULE)
    except ImportError:
        return click.ClickException
    return getattr(vendored, "ClickException", click.ClickException)


_CLICK_EXCEPTIONS = (click.ClickException, _vendored_click_exception())


# `codebase_rag.main` imports pydantic-ai and every provider SDK, and
# `codebase_rag.vector_store` imports the Qdrant and Milvus clients; together
# they cost over a second, and every `cgr` invocation imports this module
# (issue #2253). These wrappers defer those imports to the commands that need
# them and keep the names patchable attributes of `cli`.
def clear_all_embeddings(*args: Any, **kwargs: Any) -> None:
    from .vector_store import clear_all_embeddings as impl

    return impl(*args, **kwargs)


def delete_project_embeddings(*args: Any, **kwargs: Any) -> bool:
    from .vector_store import delete_project_embeddings as impl

    return impl(*args, **kwargs)


def _import_vector_store() -> None:
    # The embedding wrappers import lazily. A clean run resolves the module
    # before it wipes anything, so a broken install stops with the graph
    # intact rather than after `clean_database`.
    importlib.import_module(".vector_store", __package__)


def update_model_settings(*args: Any, **kwargs: Any) -> None:
    from .main import update_model_settings as impl

    return impl(*args, **kwargs)


def prompt_for_unignored_directories(*args: Any, **kwargs: Any) -> frozenset[str]:
    from .main import prompt_for_unignored_directories as impl

    return impl(*args, **kwargs)


def export_graph_to_file(*args: Any, **kwargs: Any) -> bool:
    from .main import export_graph_to_file as impl

    return impl(*args, **kwargs)


def _create_configuration_table(*args: Any, **kwargs: Any) -> Table:
    from .main import _create_configuration_table as impl

    return impl(*args, **kwargs)


def main_single_query(*args: Any, **kwargs: Any) -> None:
    from .main import main_single_query as impl

    return impl(*args, **kwargs)


def main_async(*args: Any, **kwargs: Any) -> Coroutine[Any, Any, None]:
    from .main import main_async as impl

    return impl(*args, **kwargs)


def main_optimize_async(*args: Any, **kwargs: Any) -> Coroutine[Any, Any, None]:
    from .main import main_optimize_async as impl

    return impl(*args, **kwargs)


class _CgrGroup(TyperGroup):
    def invoke(self, ctx: "_GroupContext") -> object:
        # Every command reaches the graph through the ingestor, which raises
        # this at connect time with the fix in its message; printing it here
        # covers them all, the delegated groups and the chat included (#2443).
        try:
            result = super().invoke(ctx)
        except GraphUnavailableError as e:
            app_context.console.print(style(str(e), cs.Color.RED))
            raise typer.Exit(1) from e
        # Only a command that finished gets here: a failure exits by raising.
        pledge = sponsor_pledge(ctx.invoked_subcommand, sys.stderr.isatty())
        if pledge:
            # Soft-wrapped so the terminal, not Rich, breaks the line, and
            # the URL stays one clickable link.
            terminal_aware_console(stderr=True).print(
                dim(pledge), highlight=False, soft_wrap=True
            )
        return result


# Help pages, and the MCP server, which speaks its protocol until the client
# hangs up, are not runs that finished.
_NO_SPONSOR_PLEDGE = frozenset({ch.CLICommandName.HELP, ch.CLICommandName.MCP_SERVER})


def sponsor_pledge(command: str | None, terminal: bool) -> str | None:
    """The GitHub Sponsors line a finished command ends on, or None where it
    would be in the way: off a terminal (a script or a log reading stderr),
    under `--quiet` or CGR_SPONSOR_PLEDGE=false, and after the commands in
    `_NO_SPONSOR_PLEDGE`. On stderr, so it never mixes into a command's
    output on stdout."""
    if (
        not terminal
        or settings.QUIET
        or not settings.SPONSOR_PLEDGE
        or command in _NO_SPONSOR_PLEDGE
    ):
        return None
    return cs.CLI_MSG_SPONSOR_PLEDGE.format(url=cs.SPONSOR_URL)


app = typer.Typer(
    name=cs.PACKAGE_NAME,
    help=ch.APP_DESCRIPTION,
    epilog=ch.APP_EPILOG,
    no_args_is_help=True,
    add_completion=False,
    cls=_CgrGroup,
)


def _version_callback(value: bool) -> None:
    if value:
        app_context.console.print(
            cs.CLI_MSG_VERSION.format(
                package=cs.PACKAGE_NAME, version=get_version(cs.PACKAGE_NAME)
            ),
            highlight=False,
        )
        for line in credits_lines():
            app_context.console.print(line, highlight=False)
        app_context.console.print(
            cs.CLI_MSG_SPONSOR.format(url=cs.SPONSOR_URL), highlight=False
        )
        raise typer.Exit()


def credits_lines(
    frozen: bool | None = None, executable: str | None = None
) -> list[str]:
    """Where the third-party credits are: the docs page, and for a release
    binary the notices file shipped beside it (issue #2175)."""
    lines = [cs.CLI_MSG_CREDITS.format(url=cs.CREDITS_URL)]
    if frozen if frozen is not None else getattr(sys, "frozen", False):
        stem = Path(executable or sys.executable).stem
        lines.append(
            cs.CLI_MSG_CREDITS_NOTICES.format(
                name=f"{stem}{cs.THIRD_PARTY_NOTICES_SUFFIX}"
            )
        )
    return lines


def validate_models_early() -> None:
    try:
        orchestrator_config = settings.active_orchestrator_config
        orchestrator_config.validate_api_key(cs.ModelRole.ORCHESTRATOR)

        cypher_config = settings.active_cypher_config
        cypher_config.validate_api_key(cs.ModelRole.CYPHER)
    except ValueError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e


def _update_and_validate_models(orchestrator: str | None, cypher: str | None) -> None:
    try:
        update_model_settings(orchestrator, cypher)
    except ValueError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e

    validate_models_early()


def _refuse_invalid_settings(command: str | None) -> None:
    # Checked here rather than when the settings load, so `--version`,
    # `--help` and `cgr help` (all handled before or without a command) still
    # work, and no command runs on the default a refused value fell back to.
    if not settings_errors or command == ch.CLICommandName.HELP:
        return
    for problem in settings_errors:
        typer.echo(cs.CLI_ERR_INVALID_SETTING.format(problem=problem), err=True)
    raise typer.Exit(cs.CLI_EXIT_USAGE_ERROR)


@app.callback()
def _global_options(
    ctx: typer.Context,
    version: bool | None = typer.Option(
        None,
        "--version",
        "-v",
        help=ch.HELP_VERSION,
        callback=_version_callback,
        is_eager=True,
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help=ch.HELP_QUIET,
        is_eager=True,
    ),
) -> None:
    _refuse_invalid_settings(ctx.invoked_subcommand)
    settings.QUIET = quiet
    if quiet:
        logger.remove()
        logger.add(
            sys.stderr,
            level="ERROR",
            backtrace=False,
            diagnose=False,
        )
    else:
        _default_log_level_to_info()


def _default_log_level_to_info() -> None:
    if cs.ENV_LOGURU_LEVEL in os.environ:
        return
    try:
        logger.remove(cs.LOGURU_DEFAULT_HANDLER_ID)
    except ValueError:
        return
    logger.add(sys.stderr, level=cs.LOG_LEVEL_INFO, backtrace=False, diagnose=False)


def _info(msg: str) -> None:
    if not settings.QUIET:
        app_context.console.print(msg)


def _pre_chat_sync(
    workspace_config: WorkspaceConfig | None,
    repo_sync: Callable[[], None],
    batch_size: int,
    exclude: list[str] | None,
    capture: list[str] | None,
    no_embeddings: bool,
    assume_yes: bool = False,
) -> tuple[Callable[[], None], str]:
    # The sync to run before the chat opens: every workspace repo when a
    # workspace is active, else just the target repo.
    if workspace_config is None:
        return repo_sync, cs.MSG_SYNCING_KNOWLEDGE_GRAPH
    _exit_if_workspace_names_unstorable(workspace_config)
    workspace_sync = partial(
        _sync_workspace,
        workspace_config,
        batch_size,
        exclude,
        capture=capture,
        skip_embeddings=no_embeddings or None,
        assume_yes=assume_yes,
    )
    return workspace_sync, cs.MSG_SYNCING_WORKSPACE.format(
        name=workspace_config.name, count=len(workspace_config.repos)
    )


def _exit_if_workspace_names_unstorable(workspace_config: WorkspaceConfig) -> None:
    # A workspace file written before names were checked can still hold a
    # dotted one; syncing it would merge nodes across projects (#2412).
    for repo in workspace_config.repos:
        if (error := project_name_error(repo.project_name)) is not None:
            _exit_with_error(
                cs.CLI_ERR_WORKSPACE_PROJECT_NAME.format(
                    workspace=workspace_config.name, path=repo.path, error=error
                )
            )


def _exit_with_error(message: str) -> NoReturn:
    app_context.console.print(style(message, cs.Color.RED))
    raise typer.Exit(1)


def _refuse_options_update_graph_drops(
    update_graph: bool,
    ask_agent: str | None,
    output_format: cs.QueryFormat,
    no_sync: bool,
    projects: str | None,
    *,
    workspace_given: bool,
) -> None:
    # `--update-graph` returns once its sync is done, before anything that
    # reads these, so they were accepted and dropped with exit 0 (#2478).
    # With `--workspace` it syncs the workspace's repositories and then opens
    # the assistant, which reads `-a`, `--output-format` and `--projects`, so
    # only `--no-sync` is refused there, by its own message: a sync asked for
    # with `--no-sync` contradicts itself (#2418).
    if not update_graph:
        return
    if workspace_given:
        if no_sync:
            _exit_with_error(cs.CLI_ERR_WORKSPACE_UPDATE_NO_SYNC)
        return
    for option, given, remedy in (
        (cs.CLI_OPT_ASK_AGENT, ask_agent is not None, cs.CLI_REMEDY_DROP_UPDATE_GRAPH),
        (
            cs.CLI_OPT_OUTPUT_FORMAT_JSON,
            output_format == cs.QueryFormat.JSON,
            cs.CLI_REMEDY_DROP_UPDATE_GRAPH,
        ),
        (cs.CLI_OPT_NO_SYNC, no_sync, cs.CLI_REMEDY_SYNC_OR_NO_SYNC),
        (cs.CLI_OPT_PROJECTS, projects is not None, cs.CLI_REMEDY_DROP_UPDATE_GRAPH),
    ):
        if given:
            _exit_with_error(
                cs.CLI_ERR_UPDATE_GRAPH_CONFLICT.format(option=option, remedy=remedy)
            )


def _start_update_graph(
    repo: Path,
    project_name: str,
    *,
    workspace_config: WorkspaceConfig | None = None,
    project_named: bool,
    batch_size: int,
    exclude: list[str] | None,
    interactive_setup: bool,
    clean: bool,
    output: str | None,
    capture: list[str] | None,
    skip_embeddings: bool | None,
    assume_yes: bool,
    defer_workspace_sync: bool = False,
) -> bool:
    """`start --update-graph`: sync, and say whether the assistant opens next.

    A repository is synced and the command stops, as it always has. With
    --workspace, each of the workspace's repositories is synced under its own
    name, the set the chat's own sync uses, and the assistant then opens
    scoped to the workspace (#2418). `--repo-path`, defaulting to the current
    directory, is not one of them. With `-a` that sync is deferred: the
    single query runs it, reporting on stderr so that stdout holds only the
    answer (see `_keep_stdout_for_the_answer`).
    """
    if workspace_config is not None:
        # The same check the chat's own workspace sync makes (#2412).
        _exit_if_workspace_names_unstorable(workspace_config)
        if not defer_workspace_sync:
            _sync_workspace(
                workspace_config,
                batch_size,
                exclude,
                capture=capture,
                skip_embeddings=skip_embeddings,
                assume_yes=assume_yes,
            )
            _info(style(cs.CLI_MSG_GRAPH_UPDATED, cs.Color.GREEN))
        return True
    _info(style(cs.CLI_MSG_UPDATING_GRAPH.format(path=repo), cs.Color.GREEN))
    if not interactive_setup:
        _info(style(cs.CLI_MSG_AUTO_EXCLUDE, cs.Color.YELLOW))
    try:
        _run_graph_sync(
            repo=repo,
            project_name=project_name,
            project_named=project_named,
            batch_size=batch_size,
            exclude=exclude,
            interactive_setup=interactive_setup,
            clean=clean,
            output=output,
            capture=capture,
            skip_embeddings=skip_embeddings,
            assume_yes=assume_yes,
        )
    except KeyboardInterrupt as stop:
        # Ctrl+C is a request, not a crash (#2442): one line when it left the
        # graph partial, and the shell's interrupt status, set here so it
        # does not hinge on how typer happens to handle a KeyboardInterrupt.
        if isinstance(stop, ex.SyncInterrupted):
            app_context.console.print(
                style(
                    cs.CLI_MSG_SYNC_INTERRUPTED.format(project=project_name),
                    cs.Color.YELLOW,
                )
            )
        raise typer.Exit(cs.CLI_EXIT_INTERRUPTED) from stop
    _info(style(cs.CLI_MSG_GRAPH_UPDATED, cs.Color.GREEN))
    return False


def _clean_database_only(
    repo_to_clean: Path, batch_size: int, project_name: str, assume_yes: bool
) -> None:
    # `--clean` without `--update-graph`: wipe the graph, the vector store and
    # the hash cache, then stop.
    _import_vector_store()
    # Held until the embeddings and the hash cache are gone too: a sync let
    # in after the wipe would publish both, and the clean would then delete
    # them under that sync's new graph (review of PR 2512).
    with _sync_lock_or_exit(repo_to_clean, project_name):
        with connect_memgraph(batch_size) as ingestor:
            _confirm_destructive_clean(ingestor, project_name, assume_yes)
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()

        clear_all_embeddings()
        _delete_hash_cache(repo_to_clean)
    _info(style(cs.CLI_MSG_CLEAN_DONE, cs.Color.GREEN))


def _session_project(
    workspace_config: WorkspaceConfig | None,
    target_repo_path: str,
    resolved_project_name: str,
    project_name: str | None,
) -> tuple[str, bool]:
    # The project the session's writes re-ingest into: a workspace syncs a
    # repo under the name it was registered with, whatever --project-name
    # says, so that name is the one in the graph (bot review on PR #2986).
    if workspace_config is not None and (
        repo := workspace_config.find_repo(target_repo_path)
    ):
        return repo.project_name, repo.project_named
    return resolved_project_name, project_name is not None


def _start_active_projects(
    workspace_config: WorkspaceConfig | None,
    projects: str | None,
    resolved_project_name: str,
) -> list[str]:
    if workspace_config is None:
        return _resolve_active_projects(projects, resolved_project_name)
    active_projects = workspace_config.project_names()
    if projects:
        # A workspace with no repos has no first project to fall back on, so
        # an explicit --projects list falls back to the resolved project.
        default_project = (
            active_projects[0] if active_projects else resolved_project_name
        )
        return _resolve_active_projects(projects, default_project)
    return active_projects


@contextmanager
def _console_on_stderr() -> Generator[None]:
    console = app_context.console
    app_context.console = terminal_aware_console(stderr=True)
    try:
        yield
    finally:
        app_context.console = console


def _keep_stdout_for_the_answer[**P](command: Callable[P, None]) -> Callable[P, None]:
    # `-a` prints its answer, and only that, on stdout, which `main_single_query`
    # keeps clean of its logs for a script to read: the command's warnings,
    # its sync's progress (Greptile review of PR 2507) and the query's result
    # tables go to stderr, from the first check until the command ends
    # (CodeRabbit review of PR 2507). A decorator rather than a `typer.Context`
    # parameter, which `start` has too many of already (Sonar S107).
    @wraps(command)
    def run(*args: P.args, **kwargs: P.kwargs) -> None:
        if not kwargs.get("ask_agent"):
            command(*args, **kwargs)
            return
        with _console_on_stderr():
            command(*args, **kwargs)

    return run


def _run_single_query(
    target_repo_path: str,
    batch_size: int,
    question: str,
    active_projects: list[str] | None,
    output_format: cs.QueryFormat,
    sync_task: Callable[[], None] | None,
) -> None:
    if sync_task is not None:
        sync_task()
    main_single_query(
        target_repo_path,
        batch_size,
        question,
        active_projects=active_projects,
        output_format=output_format,
    )


def _launch_session(
    target_repo_path: str,
    batch_size: int,
    ask_agent: str | None,
    active_projects: list[str],
    output_format: cs.QueryFormat,
    sync_task: Callable[[], None] | None,
    sync_message: str,
    project_name: str | None = None,
    project_named: bool = False,
) -> None:
    # `-a` answers one question and exits; otherwise open the chat loop.
    try:
        if ask_agent:
            _run_single_query(
                target_repo_path,
                batch_size,
                ask_agent,
                active_projects,
                output_format,
                sync_task,
            )
        else:
            asyncio.run(
                main_async(
                    target_repo_path,
                    batch_size,
                    active_projects=active_projects,
                    show_config_table=False,
                    pre_chat_sync=sync_task,
                    pre_chat_sync_message=sync_message,
                    project_name=project_name,
                    project_named=project_named,
                )
            )
    except KeyboardInterrupt:
        app_context.console.print(style(cs.CLI_MSG_APP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_STARTUP.format(error=e), cs.Color.RED)
        )


def _sync_before_session(no_sync: bool, update_graph: bool, answering: bool) -> bool:
    # Past `_start_update_graph`, `update_graph` means a workspace's, which
    # has synced every repository already unless `-a` deferred that sync.
    return not no_sync and (answering or not update_graph)


def _exit_if_output_unusable(output: str | None, update_graph: bool) -> None:
    if output and not update_graph:
        _exit_with_error(cs.CLI_ERR_OUTPUT_REQUIRES_UPDATE)
    if output:
        _exit_if_unwritable(output)


def _refuse_unsupported_workspace_update(
    workspace_config: WorkspaceConfig | None,
    update_graph: bool,
    *,
    clean: bool,
    output: str | None,
    interactive_setup: bool,
) -> None:
    # Each is defined for one repository: `--clean` wipes the graph before
    # that repository's sync, `-o` writes that sync's result, and the setup
    # prompt asks about that repository's directories.
    if workspace_config is None or not update_graph:
        return
    for option, given in (
        (cs.CLI_OPT_CLEAN, clean),
        (cs.CLI_OPT_OUTPUT, output is not None),
        (cs.CLI_OPT_INTERACTIVE_SETUP, interactive_setup),
    ):
        if given:
            _exit_with_error(cs.CLI_ERR_WORKSPACE_UPDATE_OPTION.format(option=option))


def _refuse_empty_workspace(
    workspace_config: WorkspaceConfig | None,
    update_graph: bool,
    active_projects: list[str],
) -> None:
    # A workspace with no repositories gives `--update-graph` nothing to sync,
    # and leaves the assistant's scope empty unless `--projects` names one: an
    # empty scope reads as every project in the shared graph (Greptile review
    # of PR 2507). Refused before the stack starts or anything is synced.
    if workspace_config is None or workspace_config.repos:
        return
    if update_graph or not active_projects:
        _exit_with_error(
            cs.CLI_ERR_START_EMPTY_WORKSPACE.format(name=workspace_config.name)
        )


def _load_workspace_or_exit(workspace: str | None) -> WorkspaceConfig | None:
    if workspace is None:
        return None
    try:
        return load_workspace(workspace)
    except WorkspaceError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e


def _sync_workspace(
    config: WorkspaceConfig,
    batch_size: int,
    exclude: list[str] | None,
    capture: list[str] | None = None,
    skip_embeddings: bool | None = None,
    assume_yes: bool = False,
) -> None:
    total = len(config.repos)
    if total == 0:
        _info(
            style(cs.CLI_MSG_WORKSPACE_EMPTY.format(name=config.name), cs.Color.YELLOW)
        )
        return
    _info(
        style(
            cs.CLI_MSG_WORKSPACE_SYNCING.format(name=config.name, count=total),
            cs.Color.CYAN,
        )
    )
    for idx, repo in enumerate(config.repos, start=1):
        repo_path = repo.repo_path()
        _info(
            style(
                cs.CLI_MSG_WORKSPACE_SYNC_REPO.format(
                    idx=idx,
                    total=total,
                    path=repo_path,
                    project_name=repo.project_name,
                ),
                cs.Color.CYAN,
            )
        )
        _run_graph_sync(
            repo=repo_path,
            project_name=repo.project_name,
            project_named=repo.project_named,
            batch_size=batch_size,
            exclude=exclude,
            interactive_setup=False,
            capture=capture,
            skip_embeddings=skip_embeddings,
            assume_yes=assume_yes,
        )


def _resolve_active_projects(projects: str | None, default_project: str) -> list[str]:
    if projects:
        parsed = [p.strip() for p in projects.split(",") if p.strip()]
        if parsed:
            return parsed
    return [default_project]


def _maybe_start_stack(skip_embeddings: bool = False) -> None:
    # The Docker stack serves Memgraph, and Qdrant when the vector settings
    # pick the bundled one. The embedded graph is a file and Neo4j is the
    # user's own server, so for them the stack runs only for the vectors:
    # only its Qdrant is started and checked, since every Memgraph check
    # needs a driver those installs need not have. A run that writes no
    # embeddings, or writes them to another backend, needs no Qdrant, so it
    # never starts one: for the embedded graph that means no Docker at all.
    wants_vectors = not (skip_embeddings or settings.SKIP_EMBEDDINGS)
    if settings.GRAPH_BACKEND == DIALECT_MEMGRAPH:
        mgr = (
            StackManager()
            if wants_vectors
            and settings.VECTOR_STORE_BACKEND == cs.VectorStoreBackend.QDRANT
            else StackManager(services=(SERVICE_MEMGRAPH,))
        )
    else:
        if not wants_vectors:
            return
        mgr = StackManager(services=(SERVICE_QDRANT,))
        if not mgr.serves_vectors():
            return
    try:
        mgr.locate_published_qdrant()
        if mgr.status().state == StackState.RUNNING:
            # This early return bypasses ensure_running, so it needs its own
            # public-port (issue #1380) and authentication checks for a stack
            # that is already up.
            mgr.warn_if_ports_are_public()
            mgr.raise_if_auth_not_enforced()
            return
        mgr.ensure_running()
    except StackError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e


def _capture_selection(capture: list[str] | None) -> CaptureSelection:
    # Env CGR_CAPTURE is the sticky baseline; --capture tokens are appended so
    # a single run can override it (later tokens win in the resolver).
    return resolve_capture(
        [*split_spec(settings.CGR_CAPTURE), *_capture_tokens(capture)]
    )


def _capture_tokens(capture: list[str] | None) -> list[str]:
    # A flag value splits like CGR_CAPTURE, so `--capture none,structure` works.
    return [token for value in capture or [] for token in split_spec(value)]


def _known_capture(capture: list[str] | None) -> list[str] | None:
    if unknown := unknown_tokens(_capture_tokens(capture)):
        raise typer.BadParameter(
            cs.CLI_ERR_CAPTURE_UNKNOWN.format(
                tokens=cs.SEPARATOR_COMMA_SPACE.join(unknown),
                groups=cs.SEPARATOR_COMMA_SPACE.join(g.value for g in cs.CaptureGroup),
            )
        )
    return capture


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _projects_in_graph(ingestor: MemgraphIngestor) -> list[str] | None:
    """Every project in the graph, or None when the graph cannot be read.

    None is NOT an empty graph: treating a failed enumeration as "no other
    projects" would skip the confirmation and wipe every project precisely
    when we cannot say what would be lost.
    """
    try:
        return sorted(ingestor.list_projects())
    except Exception as exc:
        logger.warning(ls.MG_LIST_PROJECTS_FAILED.format(error=exc))
        return None


def _confirm_destructive_clean(
    ingestor: MemgraphIngestor, project_name: str, assume_yes: bool
) -> None:
    """Abort unless the user accepts losing every other project in the graph."""
    if assume_yes:
        return

    # `--clean` deletes the whole graph, so the prompt counts every project in
    # it. Deriving that count from `others` would understate it by one whenever
    # this project has not been synced yet and so is not in the graph.
    projects = _projects_in_graph(ingestor)
    if projects is None:
        # Fail closed: an unreadable project list cannot show what the wipe
        # would destroy, so it must not be read as an empty graph.
        app_context.console.print(
            style(cs.CLI_ERR_CLEAN_UNKNOWN_PROJECTS, cs.Color.RED)
        )
        raise typer.Exit(1)

    others = [name for name in projects if name != project_name]
    if not others:
        return

    app_context.console.print(
        style(
            cs.CLI_WARN_CLEAN_OTHER_PROJECTS.format(
                project_name=project_name,
                count=len(others),
                projects=", ".join(others),
            ),
            cs.Color.YELLOW,
        )
    )

    if not _stdin_is_interactive():
        app_context.console.print(
            style(
                cs.CLI_ERR_CLEAN_NEEDS_CONFIRMATION.format(project_name=project_name),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)

    confirmed = typer.confirm(
        cs.CLI_PROMPT_CLEAN_CONFIRM.format(count=len(projects)), default=False
    )
    if not confirmed:
        app_context.console.print(style(cs.CLI_MSG_CLEAN_ABORTED, cs.Color.CYAN))
        raise typer.Exit(1)


def _exit_if_project_owned_elsewhere(
    batch_size: int, project_name: str, repo: Path, *, clean: bool, assume_yes: bool
) -> _OwnerCheck:
    """Stop before the sync when `_project_owner_refusal` refuses it (#2411).

    A `--clean` rebuild is not checked: it wipes every project in the graph,
    and `_confirm_destructive_clean` already asks before it does.
    """
    if clean:
        return _OwnerCheck(None)
    # On its own connection: exiting inside the sync's would report the
    # refusal as a failed session.
    with connect_memgraph(batch_size) as ingestor:
        check = _project_owner_refusal(ingestor, project_name, repo, assume_yes)
    if check.refusal is not None:
        _exit_with_error(check.refusal)
    return check


def _release_project_claim(batch_size: int, project_name: str, repo: Path) -> None:
    # On its own connection, and best effort like the release itself: the
    # sync's own failure is what is being reported.
    try:
        with connect_memgraph(batch_size) as ingestor:
            release_project_claim(ingestor, project_name, repo)
    except Exception as exc:
        logger.warning(
            ls.PROJECT_CLAIM_RELEASE_FAILED.format(project_name=project_name, error=exc)
        )


@contextmanager
def _project_claim(
    batch_size: int, project_name: str, repo: Path, *, clean: bool, assume_yes: bool
) -> Generator[list[str] | None]:
    """Hold this sync's claim on its project name while the sync runs (#2411).

    Yields the roots `_mark_sync_incomplete` must still find on the Project.
    A claim the sync created is given back when the sync fails before its
    incomplete-run marker or any code is written; the release query checks
    that, so a failure further in keeps it (review of PR 2499).
    """
    check = _exit_if_project_owned_elsewhere(
        batch_size, project_name, repo, clean=clean, assume_yes=assume_yes
    )
    try:
        yield check.owned_roots
    except BaseException:
        if check.created:
            _release_project_claim(batch_size, project_name, repo)
        raise


def _mark_sync_incomplete(
    ingestor: MemgraphIngestor,
    project_name: str,
    owner: dict[str, PropertyValue],
    owned_roots: list[str] | None = None,
) -> None:
    """Put the `:IncompleteRun` marker down before the sync writes, or exit.

    `mark_sync_incomplete` says why a refused marker stops the sync (#2219)
    and what `owned_roots` holds it to (review of PR 2499).
    """
    try:
        mark_sync_incomplete(ingestor, project_name, owner, owned_roots)
    except SyncRefusedError as refused:
        app_context.console.print(style(str(refused), cs.Color.RED))
        raise typer.Exit(1) from refused


@contextmanager
def _sync_lock_or_exit(repo: Path, project_name: str) -> Generator[None]:
    """Hold the checkout's sync lock for the whole sync, or stop first (#2441).

    Taken before the `--clean` wipe and the `:IncompleteRun` marker, so a
    run refused here has changed nothing: the running sync keeps its graph
    and its marker, and no marker is left behind for a run that never began.
    """
    held = ExitStack()
    try:
        held.enter_context(repo_sync_lock(repo, project_name))
    except SyncLockError as refused:
        app_context.console.print(style(str(refused), cs.Color.RED))
        raise typer.Exit(1) from None
    with held:
        yield


def _is_home_or_root(repo: Path) -> bool:
    """Whether a sync of `repo` would crawl a whole home directory or disk.

    `--repo-path` defaults to the current directory, so a sync started from
    `$HOME` indexed every file under it (#2418).
    """
    resolved = repo.resolve()
    return resolved == Path.home().resolve() or resolved == resolved.parent


def _exit_if_syncing_home_or_root(repo: Path, assume_yes: bool) -> None:
    # A home directory or disk root is synced only when `--yes` says so.
    if not assume_yes and _is_home_or_root(repo):
        _exit_with_error(cs.CLI_ERR_SYNC_HOME_OR_ROOT.format(path=repo.resolve()))


def _stopped_sync_token(owner: ResultRow) -> str | None:
    """The marker's owner token if the CLI sync that wrote it provably stopped.

    Only a sync in this process table can be checked: one whose process is
    gone, or one of this process's own that has returned. A marker from
    another host, from a live process, or from before markers named their
    owner is kept, since it may guard a graph that sync is still writing.
    """
    pid = owner.get(cs.KEY_OWNER_PID)
    token = owner.get(cs.KEY_OWNER_TOKEN)
    if (
        owner.get(cs.KEY_OWNER_HOST) != process_host()
        or not isinstance(pid, int)
        or not isinstance(token, str)
    ):
        return None
    if pid == os.getpid():
        stopped = token not in _RUNNING_SYNC_TOKENS
    else:
        stopped = not pid_may_be_running(pid)
    return token if stopped else None


def _clear_stopped_sync_marker(ingestor: MemgraphIngestor, project_name: str) -> bool:
    """Clear the CLI sync's marker if its sync stopped; True if one is kept."""
    params = _sync_marker_params(project_name)
    kept = False
    for owner in ingestor.fetch_all(cq.CYPHER_CLI_SYNC_MARKER_OWNER, params):
        if (token := _stopped_sync_token(owner)) is None:
            kept = True
            continue
        ingestor.execute_write(
            cq.CYPHER_CLEAR_STOPPED_CLI_SYNC_MARKER,
            params | {cs.KEY_OWNER_TOKEN: token},
        )
    return kept


def _clear_deleted_project_markers(
    ingestor: MemgraphIngestor, project_name: str
) -> None:
    """Take off the markers a deleted project no longer needs (#2444 review).

    A marker sits on its own node, out of the delete's reach, so without this
    a sync that failed before the delete keeps the project in `cgr status`
    as interrupted. A marker goes only when its run cannot still be writing:
    the CLI sync's once that sync provably stopped (PR #2532 review: a sync
    still running would otherwise write on unguarded), and any run's that
    never wrote the graph. A run that had begun writing owns its marker
    (#1709). Best effort, like `_clear_sync_incomplete`: the project is
    deleted.
    """
    try:
        kept = _clear_stopped_sync_marker(ingestor, project_name)
        ingestor.execute_write(
            cq.CYPHER_RECOVER_PROJECT_INCOMPLETE,
            {cs.KEY_PROJECT_NAME: project_name},
        )
    except Exception as exc:
        logger.warning(
            ls.CLI_DELETE_MARKER_NOT_CLEARED.format(project=project_name, error=exc)
        )
        return
    if kept:
        logger.warning(ls.CLI_DELETE_SYNC_MARKER_KEPT.format(project=project_name))


def _sweep_glosses_after_failed_delete(
    ingestor: MemgraphIngestor, project_name: str
) -> None:
    """Sweep the notes of a delete that raised after the project was removed.

    The delete commits statement by statement, so a later cleanup can raise
    once the project is already gone, and a retry then stops at "project not
    found" before any sweep. A project still listed keeps its unattached
    notes: they are LOST or AMBIGUOUS by design and readable on their old
    names. Never raises, so the delete's own error is what is reported.
    """
    try:
        still_listed = project_name in ingestor.list_projects()
    except Exception as exc:
        logger.warning(
            ls.CLI_DELETE_GLOSS_SWEEP_SKIPPED.format(project=project_name, error=exc)
        )
        return
    if not still_listed:
        prune_orphaned_glosses(ingestor, project_name)


def _run_updater_deferring_interrupt(
    updater: "GraphUpdater", *, rebuild: bool = False
) -> KeyboardInterrupt | None:
    """Run the sync's graph update; return a Ctrl+C that landed after its commit.

    The run stamps the sync time as part of its commit (#2444), and the
    caller clears its marker before re-raising what this returns; a Ctrl+C
    before the commit raises `SyncInterrupted` instead. `rebuild` forces a
    full re-index over runs another writer left part-way.
    """
    try:
        updater.run(force=rebuild)
    except ex.EmbeddingsInterrupted as stop:
        # Raised only after the run committed, so the graph is whole and
        # the sync is recorded like any other; the interrupt then ends
        # the command outside the connection, which would otherwise log
        # it as a failed write.
        return stop
    except KeyboardInterrupt as stop:
        # Decided by whether the run committed, not by where the interrupt
        # surfaced: a Ctrl+C between the commit and the return found the
        # graph whole, and must be recorded and unmarked the same way.
        if not updater.committed:
            # Stopped short of the commit: the marker stays down, and only
            # the caller knows whether to say so.
            raise ex.SyncInterrupted from stop
        return stop
    return None


def _run_graph_sync(
    repo: Path,
    project_name: str,
    project_named: bool,
    batch_size: int,
    exclude: list[str] | None,
    interactive_setup: bool,
    clean: bool = False,
    output: str | None = None,
    capture: list[str] | None = None,
    skip_embeddings: bool | None = None,
    assume_yes: bool = False,
) -> None:
    # Resolved before any graph write: see `_import_vector_store`.
    from .graph_updater import GraphUpdater

    _exit_if_syncing_home_or_root(repo, assume_yes)

    # Once, as the updater would: the ownership check, the marker and the
    # write below must all name the same project.
    project_name = normalize_project_name(project_name, repo)
    if clean:
        _import_vector_store()

    cgrignore = load_ignore_patterns(repo)
    cli_excludes = frozenset(exclude) if exclude else frozenset()
    exclude_paths = cli_excludes | cgrignore.exclude or None
    unignore_paths: frozenset[str] | None
    if interactive_setup:
        unignore_paths = prompt_for_unignored_directories(repo, exclude)
    else:
        unignore_paths = cgrignore.unignore or None

    elapsed = time.monotonic()
    with (
        _sync_lock_or_exit(repo, project_name),
        _project_claim(
            batch_size, project_name, repo, clean=clean, assume_yes=assume_yes
        ) as owned_roots,
        connect_memgraph(batch_size) as ingestor,
        _sync_owner() as owner,
    ):
        if clean:
            _confirm_destructive_clean(ingestor, project_name, assume_yes)
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()
            _delete_hash_cache(repo)
            # Stale vectors keyed by recycled node ids would crowd out live
            # hits and can map onto unrelated nodes in the rebuilt graph.
            clear_all_embeddings()

        # Read before this sync marks: the runs whose markers its rebuild
        # supersedes (review of PR #3493).
        stranded = _stranded_runs(ingestor, project_name, cs.CLI_SYNC_RUN_ID)
        if stranded:
            logger.warning(
                ls.REBUILD_OVER_STRANDED_RUNS.format(
                    project=project_name, count=len(stranded)
                )
            )
        # After the wipe, which would delete the marker with everything else,
        # and before `ensure_constraints`, whose migration can already purge.
        _mark_sync_incomplete(ingestor, project_name, owner, owned_roots)
        ingestor.ensure_constraints()

        parsers, queries = load_parsers()

        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo,
            parsers=parsers,
            queries=queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
            project_named=project_named,
            capture=_capture_selection(capture),
            skip_embeddings=skip_embeddings,
        )
        interrupted = _run_updater_deferring_interrupt(updater, rebuild=bool(stranded))
        _clear_sync_incomplete(ingestor, project_name)
        _retire_stranded_runs(ingestor, project_name, stranded)
        # Taken before the export: counting it made a no-op sync of a small
        # repo report the time to dump the whole shared graph (issue #2440).
        elapsed = time.monotonic() - elapsed

        exported = True
        if output and interrupted is None:
            # The repo's own graph: `-o` wrote every project in the shared
            # database (issue #2440), which `cgr export` without a scope still
            # does. The updater's name is the one its Project node carries.
            project = updater.project_name
            _info(
                style(
                    cs.CLI_MSG_EXPORTING_TO.format(project=project, path=output),
                    cs.Color.CYAN,
                )
            )
            exported = export_graph_to_file(ingestor, output, [project])
    # Raised outside the `with`: the ingestor logs any exception leaving it as
    # a traceback, typer.Exit included, and the failure is already reported.
    if not exported:
        raise typer.Exit(1)
    if updater.skipped_because_in_sync:
        app_context.console.print(
            style(
                cs.CLI_MSG_SYNC_SKIPPED.format(project=project_name, elapsed=elapsed),
                cs.Color.CYAN,
                cs.StyleModifier.DIM,
            )
        )
    else:
        app_context.console.print(
            style(
                cs.CLI_MSG_SYNC_DONE.format(project=project_name, elapsed=elapsed),
                cs.Color.CYAN,
                cs.StyleModifier.NONE,
            )
        )
        _report_call_pass_failures(repo, list(updater.call_pass_failures))
    if interrupted is not None:
        raise interrupted


def _report_call_pass_failures(repo: Path, failed: list[Path]) -> None:
    # A file whose call pass failed lost edges while the sync still reads
    # "done": the error lines scroll away, so the summary counts them (#3173).
    if not failed:
        return
    names = sorted(
        path.relative_to(repo).as_posix() if path.is_relative_to(repo) else str(path)
        for path in failed
    )
    shown = names[: cs.CLI_CALL_PASS_FAILURES_SHOWN]
    if len(names) > len(shown):
        shown.append(cs.CLI_MSG_AND_MORE.format(count=len(names) - len(shown)))
    app_context.console.print(
        style(
            cs.CLI_MSG_CALL_PASS_FAILURES.format(
                count=len(names), files=", ".join(shown)
            ),
            cs.Color.YELLOW,
        )
    )


def _delete_hash_cache(repo_path: Path) -> None:
    # Resolving the state directory also moves in what an older cgr left in
    # the tree, so a clean right after upgrading clears that copy too.
    state = prepare_state_dir(repo_path)
    cache_path = state / cs.HASH_CACHE_FILENAME
    if cache_path.exists():
        _info(
            style(
                cs.CLI_MSG_CLEANING_HASH_CACHE.format(path=cache_path),
                cs.Color.YELLOW,
            )
        )
        cache_path.unlink(missing_ok=True)
    (state / cs.DIR_MTIMES_FILENAME).unlink(missing_ok=True)
    (state / cs.EXPOSES_CLEANUP_PENDING_FILENAME).unlink(missing_ok=True)
    (state / cs.PARSER_FINGERPRINT_FILENAME).unlink(missing_ok=True)
    (state / cs.EXCLUSION_STATE_FILENAME).unlink(missing_ok=True)


def _start_project_name(project_name: str | None, repo: Path) -> str:
    # Stripped as `normalize_project_name` strips it, so the ownership check
    # asks about the name the sync writes (review of PR 2499). A blank name
    # falls back to the derived one, which carries a hash of the path.
    return (project_name and project_name.strip()) or derive_project_name(repo)


def _storable_project_name(value: str | None) -> str | None:
    if value is not None and (error := project_name_error(value)) is not None:
        raise typer.BadParameter(error)
    return value


def _resolve_and_validate_repo(repo_path: str | None) -> Path:
    resolved = resolve_repo_path(repo_path, settings.TARGET_REPO_PATH)
    if not resolved.exists():
        app_context.console.print(
            style(cs.CLI_ERR_PATH_NOT_EXISTS.format(path=resolved), cs.Color.RED)
        )
        raise typer.Exit(1)
    if not resolved.is_dir():
        app_context.console.print(
            style(cs.CLI_ERR_PATH_NOT_DIR.format(path=resolved), cs.Color.RED)
        )
        raise typer.Exit(1)
    if not (resolved / cs.GIT_DIR_NAME).exists():
        app_context.console.print(
            style(cs.CLI_WARN_NOT_GIT_REPO.format(path=resolved), cs.Color.YELLOW)
        )
    return resolved


def _cleanup_project_embeddings(
    ingestor: MemgraphIngestor, project_name: str, projects: Iterable[str]
) -> None:
    rows = ingestor.fetch_all(
        cs.CYPHER_QUERY_PROJECT_NODE_IDS,
        {cs.KEY_PROJECT_NAME: project_name},
    )
    node_ids: list[int] = []
    for row in rows:
        node_id = row.get(cs.KEY_NODE_ID)
        if isinstance(node_id, int):
            node_ids.append(node_id)
    # A project nested under this one (`svc.v2` under `svc`) keeps its
    # vectors, which the prefix-scoped node-id read also names.
    delete_project_embeddings(
        project_name, node_ids, nested_project_names(project_name, projects)
    )


@app.command(
    help=ch.CMD_START,
    short_help=ch.CMD_START,
    epilog=ch.EXAMPLES_START,
    rich_help_panel=ch.PANEL_USE,
)
@_keep_stdout_for_the_answer
def start(
    repo_path: str | None = typer.Option(
        None, "--repo-path", help=ch.HELP_REPO_PATH_RETRIEVAL
    ),
    update_graph: bool = typer.Option(
        False,
        "--update-graph",
        help=ch.HELP_UPDATE_GRAPH,
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help=ch.HELP_CLEAN_DB,
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=ch.HELP_ASSUME_YES,
    ),
    output: str | None = typer.Option(
        None,
        "-o",
        "--output",
        help=ch.HELP_OUTPUT_GRAPH,
    ),
    orchestrator: str | None = typer.Option(
        None,
        "--orchestrator",
        help=ch.HELP_ORCHESTRATOR,
    ),
    cypher: str | None = typer.Option(
        None,
        "--cypher",
        help=ch.HELP_CYPHER_MODEL,
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help=ch.HELP_NO_CONFIRM,
    ),
    no_instructions: bool = typer.Option(
        False,
        "--no-instructions",
        help=ch.HELP_NO_INSTRUCTIONS,
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
    ),
    project_name: str | None = typer.Option(
        None,
        "--project-name",
        help=ch.HELP_PROJECT_NAME,
        callback=_storable_project_name,
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help=ch.HELP_EXCLUDE_PATTERNS,
    ),
    capture: list[str] | None = typer.Option(
        None,
        "--capture",
        help=capture_help(),
        callback=_known_capture,
    ),
    interactive_setup: bool = typer.Option(
        False,
        "--interactive-setup",
        help=ch.HELP_INTERACTIVE_SETUP,
    ),
    ask_agent: str | None = typer.Option(
        None,
        "-a",
        "--ask-agent",
        help=ch.HELP_ASK_AGENT,
    ),
    output_format: cs.QueryFormat = typer.Option(
        cs.QueryFormat.TABLE,
        "--output-format",
        help=ch.HELP_QUERY_OUTPUT_FORMAT,
    ),
    no_start_stack: bool = typer.Option(
        False,
        "--no-start-stack",
        help=ch.HELP_NO_START_STACK,
    ),
    no_sync: bool = typer.Option(
        False,
        "--no-sync",
        help=ch.HELP_NO_SYNC,
    ),
    no_embeddings: bool = typer.Option(
        False,
        "--no-embeddings",
        help=ch.HELP_NO_EMBEDDINGS,
    ),
    projects: str | None = typer.Option(
        None,
        "--projects",
        help=ch.HELP_PROJECTS,
    ),
    workspace: str | None = typer.Option(
        None,
        "--workspace",
        help=ch.HELP_WORKSPACE,
    ),
) -> None:
    app_context.session.confirm_edits = not no_confirm
    app_context.session.load_cgr_instructions = not no_instructions

    _refuse_options_update_graph_drops(
        update_graph,
        ask_agent,
        output_format,
        no_sync,
        projects,
        workspace_given=workspace is not None,
    )
    if output_format == cs.QueryFormat.JSON and not ask_agent:
        _exit_with_error(cs.CLI_ERR_JSON_REQUIRES_ASK_AGENT)

    resolved_repo = _resolve_and_validate_repo(repo_path)
    target_repo_path = str(resolved_repo)
    resolved_project_name = _start_project_name(project_name, resolved_repo)

    _exit_if_output_unusable(output, update_graph)

    workspace_config = _load_workspace_or_exit(workspace)
    _refuse_unsupported_workspace_update(
        workspace_config,
        update_graph,
        clean=clean,
        output=output,
        interactive_setup=interactive_setup,
    )
    active_projects = _start_active_projects(
        workspace_config, projects, resolved_project_name
    )
    _refuse_empty_workspace(workspace_config, update_graph, active_projects)

    if not no_start_stack:
        _maybe_start_stack(skip_embeddings=no_embeddings)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    if clean and not update_graph:
        _clean_database_only(
            Path(target_repo_path), effective_batch_size, resolved_project_name, yes
        )
        return

    _update_and_validate_models(orchestrator, cypher)

    if not ask_agent and not update_graph:
        app_context.console.print(_create_configuration_table(target_repo_path))

    if update_graph and not _start_update_graph(
        resolved_repo,
        resolved_project_name,
        workspace_config=workspace_config,
        project_named=project_name is not None,
        batch_size=effective_batch_size,
        exclude=exclude,
        interactive_setup=interactive_setup,
        clean=clean,
        output=output,
        capture=capture,
        skip_embeddings=no_embeddings or None,
        assume_yes=yes,
        defer_workspace_sync=bool(ask_agent),
    ):
        return

    sync_task: Callable[[], None] | None = None
    sync_message = cs.MSG_SYNCING_KNOWLEDGE_GRAPH
    if _sync_before_session(no_sync, update_graph, bool(ask_agent)):
        sync_task, sync_message = _pre_chat_sync(
            workspace_config,
            partial(
                _run_graph_sync,
                repo=resolved_repo,
                project_name=resolved_project_name,
                project_named=project_name is not None,
                batch_size=effective_batch_size,
                exclude=exclude,
                interactive_setup=interactive_setup,
                capture=capture,
                skip_embeddings=no_embeddings or None,
                assume_yes=yes,
            ),
            effective_batch_size,
            exclude,
            capture,
            no_embeddings,
            assume_yes=yes,
        )

    session_project, session_named = _session_project(
        workspace_config, target_repo_path, resolved_project_name, project_name
    )
    _launch_session(
        target_repo_path,
        effective_batch_size,
        ask_agent,
        active_projects,
        output_format,
        sync_task,
        sync_message,
        project_name=session_project,
        project_named=session_named,
    )


@app.command(
    help=ch.CMD_INDEX,
    short_help=ch.CMD_INDEX,
    epilog=ch.EXAMPLES_INDEX,
    rich_help_panel=ch.PANEL_GRAPH,
)
def index(
    repo_path: str | None = typer.Option(
        None, "--repo-path", help=ch.HELP_REPO_PATH_INDEX
    ),
    output_proto_dir: str = typer.Option(
        ...,
        "-o",
        "--output-proto-dir",
        help=ch.HELP_OUTPUT_PROTO_DIR,
    ),
    split_index: bool = typer.Option(
        False,
        "--split-index",
        help=ch.HELP_SPLIT_INDEX,
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help=ch.HELP_EXCLUDE_PATTERNS,
    ),
    capture: list[str] | None = typer.Option(
        None,
        "--capture",
        help=capture_help(),
        callback=_known_capture,
    ),
    interactive_setup: bool = typer.Option(
        False,
        "--interactive-setup",
        help=ch.HELP_INTERACTIVE_SETUP,
    ),
) -> None:
    repo_to_index = _resolve_and_validate_repo(repo_path)
    _info(style(cs.CLI_MSG_INDEXING_AT.format(path=repo_to_index), cs.Color.GREEN))

    _info(style(cs.CLI_MSG_OUTPUT_TO.format(path=output_proto_dir), cs.Color.CYAN))

    cgrignore = load_ignore_patterns(repo_to_index)
    cli_excludes = frozenset(exclude) if exclude else frozenset()
    exclude_paths = cli_excludes | cgrignore.exclude or None
    unignore_paths: frozenset[str] | None = None
    if interactive_setup:
        unignore_paths = prompt_for_unignored_directories(repo_to_index, exclude)
    else:
        _info(style(cs.CLI_MSG_AUTO_EXCLUDE, cs.Color.YELLOW))
        unignore_paths = cgrignore.unignore or None

    try:
        indexed_source = source_state(Path(repo_to_index))
        capture_config = _capture_selection(capture)
        ingestor = ProtobufFileIngestor(
            output_path=output_proto_dir,
            split_index=split_index,
            repo_path=str(repo_to_index),
        )
        parsers, queries = load_parsers()
        from .graph_updater import GraphUpdater

        # The output is a fresh snapshot, not a graph that persists between
        # runs, so the sync state lives in a throwaway directory: a previous
        # run's state must never make this one incremental (it wrote a
        # Project-only index for an unchanged repo), and this run must not
        # leave state telling the next sync its live graph is current
        # (issue #2401).
        with tempfile.TemporaryDirectory(prefix=cs.INDEX_STATE_DIR_PREFIX) as state:
            updater = GraphUpdater(
                ingestor=ingestor,
                repo_path=repo_to_index,
                parsers=parsers,
                queries=queries,
                unignore_paths=unignore_paths,
                exclude_paths=exclude_paths,
                capture=capture_config,
                state_dir=Path(state),
            )
            updater.run(force=True)
        manifest_path = write_manifest(
            Path(output_proto_dir),
            indexed_source,
            capture_description(capture_config),
        )
        _info(
            style(cs.CLI_MSG_MANIFEST_WRITTEN.format(path=manifest_path), cs.Color.CYAN)
        )
        _info(style(cs.CLI_MSG_INDEXING_DONE, cs.Color.GREEN))

    except SyncLockError as refused:
        app_context.console.print(style(str(refused), cs.Color.RED))
        raise typer.Exit(1) from None
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_INDEXING.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.INDEXING_FAILED)
        raise typer.Exit(1) from e


@app.command(
    name=ch.CLICommandName.WATCH,
    help=ch.CMD_WATCH,
    short_help=ch.CMD_WATCH,
    epilog=ch.EXAMPLES_WATCH,
    rich_help_panel=ch.PANEL_GRAPH,
)
def watch_command(
    repo_path: str | None = typer.Option(
        None, "--repo-path", help=ch.HELP_REPO_PATH_WATCH
    ),
    project_name: str | None = typer.Option(
        None,
        "--project-name",
        help=ch.HELP_PROJECT_NAME_WATCH,
        callback=_storable_project_name,
    ),
    debounce: float = typer.Option(
        cs.DEFAULT_DEBOUNCE_SECONDS, "--debounce", "-d", min=0, help=ch.HELP_DEBOUNCE
    ),
    max_wait: float = typer.Option(
        cs.DEFAULT_MAX_WAIT_SECONDS, "--max-wait", "-m", min=0, help=ch.HELP_MAX_WAIT
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
    ),
    no_start_stack: bool = typer.Option(
        False,
        "--no-start-stack",
        help=ch.HELP_NO_START_STACK,
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help=ch.HELP_ASSUME_YES_WATCH,
    ),
) -> None:
    # The checkout, project name, stack and batch size are resolved exactly as
    # `cgr start` resolves them, so the watcher updates the project that
    # command syncs and queries (issues #2432, #2433).
    resolved_repo = _resolve_and_validate_repo(repo_path)
    # Refused as `cgr start` refuses it, before the stack starts: a scan of
    # `$HOME` would index all of it and watch it recursively (#2418).
    _exit_if_syncing_home_or_root(resolved_repo, yes)
    if not no_start_stack:
        _maybe_start_stack()
    # Deferred like the sync's own imports: the watcher pulls in the parsers
    # and the graph updater, which `cgr --help` should not pay for.
    from .watch import effective_max_wait, start_watcher

    try:
        start_watcher(
            resolved_repo,
            project_name=_start_project_name(project_name, resolved_repo),
            project_named=project_name is not None,
            batch_size=settings.resolve_batch_size(batch_size),
            debounce_seconds=debounce,
            max_wait_seconds=effective_max_wait(debounce, max_wait),
            assume_yes=yes,
        )
    except (GraphUnavailableError, SyncLockError, SyncRefusedError) as refused:
        app_context.console.print(style(str(refused), cs.Color.RED))
        raise typer.Exit(1) from None


@app.command(
    name="verify-index",
    help=ch.CMD_VERIFY_INDEX,
    short_help=ch.CMD_VERIFY_INDEX,
    rich_help_panel=ch.PANEL_GRAPH,
)
def verify_index_command(
    index_dir: str = typer.Option(
        ..., "-i", "--index-dir", help=ch.HELP_VERIFY_INDEX_DIR
    ),
    trusted_manifest_sha256: str | None = typer.Option(
        None,
        "--trusted-manifest-sha256",
        help=ch.HELP_TRUSTED_MANIFEST_SHA,
    ),
) -> None:
    problems = verify_index(Path(index_dir), trusted_manifest_sha256)
    if problems:
        for problem in problems:
            app_context.console.print(
                style(cs.CLI_MSG_VERIFY_PROBLEM.format(problem=problem), cs.Color.RED)
            )
        raise typer.Exit(1)
    _info(style(cs.CLI_MSG_VERIFY_OK.format(path=index_dir), cs.Color.GREEN))


@app.command(
    name="diff-index",
    help=ch.CMD_DIFF_INDEX,
    short_help=ch.CMD_DIFF_INDEX,
    rich_help_panel=ch.PANEL_GRAPH,
)
def diff_index_command(
    old_dir: str = typer.Option(..., "--old", help=ch.HELP_DIFF_OLD),
    new_dir: str = typer.Option(..., "--new", help=ch.HELP_DIFF_NEW),
    json_out: str | None = typer.Option(None, "--json-out", help=ch.HELP_DIFF_JSON_OUT),
) -> None:
    try:
        diff = diff_indexes(Path(old_dir), Path(new_dir))
    except DiffError as error:
        app_context.console.print(style(str(error), cs.Color.RED))
        raise typer.Exit(2) from error
    rendered = json.dumps(diff, indent=2, sort_keys=True)
    if json_out is not None:
        Path(json_out).write_text(rendered + "\n", encoding="utf-8")
        _info(style(cs.CLI_MSG_DIFF_WRITTEN.format(path=json_out), cs.Color.CYAN))
    else:
        app_context.console.print(rendered)
    if diff_is_empty(diff):
        _info(style(cs.CLI_MSG_DIFF_EMPTY, cs.Color.GREEN))


@app.command(
    help=ch.CMD_EXPORT,
    short_help=ch.CMD_EXPORT,
    epilog=ch.EXAMPLES_EXPORT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def export(
    output: str = typer.Option(..., "-o", "--output", help=ch.HELP_OUTPUT_PATH),
    project: list[str] = typer.Option(
        [], "--project", "-p", help=ch.HELP_EXPORT_PROJECT
    ),
    # The spelling before issue #2415, kept working for scripts.
    project_name: list[str] = typer.Option(
        [], cs.CLI_OPT_PROJECT_NAME, "-n", hidden=True
    ),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_EXPORT_REPO_PATH,
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help=ch.HELP_EXPORT_WORKSPACE
    ),
    # Retired (issue #2410), but still accepted so scripts that pass them get
    # a warning rather than click's "No such option".
    format_json: bool | None = typer.Option(None, "--json/--no-json", hidden=True),
    batch_size: int | None = typer.Option(None, "--batch-size", hidden=True),
) -> None:
    _check_retired_export_options(format_json, batch_size)
    _exit_if_unwritable(output)
    names = [*project, *project_name]
    requested = _requested_projects(
        names, _load_workspace_or_exit(workspace), repo_path
    )
    # An explicit scope that resolves to nothing (`-p ""`, an empty workspace)
    # must not fall through to the whole graph.
    if not requested and (names or workspace is not None):
        _exit_with_error(cs.CLI_ERR_EXPORT_EMPTY_SCOPE)

    _info(style(cs.CLI_MSG_CONNECTING_MEMGRAPH, cs.Color.CYAN))
    exported = False
    refusal = ""
    try:
        # An export only reads, so the write buffer's size is moot.
        with connect_memgraph(batch_size=1) as ingestor:
            if requested:
                indexed = ingestor.list_projects()
                if missing := [name for name in requested if name not in indexed]:
                    refusal = scope_refusal(missing, indexed, repo_path, ingestor)
            if not refusal:
                exported = _export_to_file(ingestor, output, requested)
    except GraphUnavailableError:
        raise
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_EXPORT_FAILED.format(error=e), cs.Color.RED)
        )
        # The one line above is the report; the traceback is for debugging.
        logger.opt(exception=e).debug(ls.EXPORT_ERROR.format(error=e))
        raise typer.Exit(1) from e

    # Exits are raised out here: typer.Exit is a RuntimeError, so inside the
    # `try` it was reported as "Failed to export graph: 1" with a traceback.
    if refusal:
        _exit_with_error(refusal)
    if not exported:
        raise typer.Exit(1)


def _check_retired_export_options(
    format_json: bool | None, batch_size: int | None
) -> None:
    match format_json:
        case False:
            _exit_with_error(cs.CLI_ERR_EXPORT_NO_JSON)
        case True:
            app_context.console.print(style(cs.CLI_WARN_EXPORT_JSON, cs.Color.YELLOW))
    if batch_size is not None:
        app_context.console.print(style(cs.CLI_WARN_EXPORT_BATCH_SIZE, cs.Color.YELLOW))


def _exit_if_unwritable(output: str) -> None:
    if problem := unwritable_output_reason(Path(output)):
        _exit_with_error(problem)


def _requested_projects(
    project_name: list[str],
    workspace_config: WorkspaceConfig | None,
    repo_path: Path | None = None,
) -> list[str]:
    # Order kept, duplicates dropped: `-p a --workspace w` where w also holds
    # `a` selects `a` once. `cgr stats` (#2391) resolves its scope the same
    # way, and `--repo-path` adds the project the repository derives to, as
    # `cgr graph` derives it (#2415).
    return list(
        dict.fromkeys(
            [name.strip() for name in project_name if name.strip()]
            + (workspace_config.project_names() if workspace_config else [])
            + ([derive_project_name(repo_path)] if repo_path else [])
        )
    )


def _export_to_file(
    ingestor: MemgraphIngestor, output: str, requested: list[str]
) -> bool:
    if requested:
        _info(
            style(
                cs.CLI_EXPORT_SCOPE.format(projects=", ".join(requested)),
                cs.Color.CYAN,
            )
        )
    _info(style(cs.CLI_MSG_EXPORTING_DATA, cs.Color.CYAN))
    return export_graph_to_file(ingestor, output, requested)


@app.command(
    help=ch.CMD_OPTIMIZE,
    short_help=ch.CMD_OPTIMIZE,
    epilog=ch.EXAMPLES_OPTIMIZE,
    rich_help_panel=ch.PANEL_USE,
)
def optimize(
    language: str = typer.Argument(
        ...,
        help=ch.HELP_LANGUAGE_ARG,
    ),
    repo_path: str | None = typer.Option(
        None, "--repo-path", help=ch.HELP_REPO_PATH_OPTIMIZE
    ),
    reference_document: str | None = typer.Option(
        None,
        "--reference-document",
        help=ch.HELP_REFERENCE_DOC,
    ),
    orchestrator: str | None = typer.Option(
        None,
        "--orchestrator",
        help=ch.HELP_ORCHESTRATOR,
    ),
    cypher: str | None = typer.Option(
        None,
        "--cypher",
        help=ch.HELP_CYPHER_MODEL,
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help=ch.HELP_NO_CONFIRM,
    ),
    no_instructions: bool = typer.Option(
        False,
        "--no-instructions",
        help=ch.HELP_NO_INSTRUCTIONS,
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
    ),
) -> None:
    app_context.session.confirm_edits = not no_confirm
    app_context.session.load_cgr_instructions = not no_instructions

    target_repo_path = str(_resolve_and_validate_repo(repo_path))

    _update_and_validate_models(orchestrator, cypher)

    try:
        asyncio.run(
            main_optimize_async(
                language,
                target_repo_path,
                reference_document,
                orchestrator,
                cypher,
                batch_size,
            )
        )
    except KeyboardInterrupt:
        app_context.console.print(style(cs.CLI_MSG_APP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_STARTUP.format(error=e), cs.Color.RED)
        )


def _mcp_server_notice(message: str) -> None:
    # On the stdio transport stdout IS the JSON-RPC stream: a diagnostic
    # there reaches the client as a malformed message and hides the cause
    # (issue #2518). stderr is where the server's logs already go. Hosts
    # capture it to a log, not a terminal, so the message is not hard-wrapped
    # there: a long repository path stays whole on one line.
    terminal_aware_console(stderr=True).print(message)


@app.command(
    name=ch.CLICommandName.MCP_SERVER,
    help=ch.CMD_MCP_SERVER,
    short_help=ch.CMD_MCP_SERVER,
    epilog=ch.EXAMPLES_MCP_SERVER,
    rich_help_panel=ch.PANEL_USE,
)
def mcp_server(
    transport: cs.MCPTransport = typer.Option(
        cs.MCPTransport.STDIO, help=ch.HELP_MCP_TRANSPORT
    ),
    host: str = typer.Option(None, help=ch.HELP_MCP_HTTP_HOST),
    port: int = typer.Option(None, help=ch.HELP_MCP_HTTP_PORT),
    workspace: str | None = typer.Option(
        None, "--workspace", help=ch.HELP_MCP_WORKSPACE
    ),
    read_only: bool = typer.Option(False, "--read-only", help=ch.HELP_MCP_READ_ONLY),
) -> None:
    # `is True`: typer passes a bool, while a direct call that omits the
    # flag passes the truthy OptionInfo default, which must not turn the
    # setting on.
    settings.MCP_READ_ONLY = settings.MCP_READ_ONLY or read_only is True
    try:
        if transport == cs.MCPTransport.HTTP:
            from codebase_rag.mcp import serve_http

            resolved_host = host or settings.MCP_HTTP_HOST
            resolved_port = port or settings.MCP_HTTP_PORT
            asyncio.run(
                serve_http(host=resolved_host, port=resolved_port, workspace=workspace)
            )
        else:
            from codebase_rag.mcp import serve_stdio

            asyncio.run(serve_stdio(workspace=workspace))
    except KeyboardInterrupt:
        _mcp_server_notice(style(cs.CLI_MSG_APP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        _mcp_server_notice(style(cs.CLI_ERR_CONFIG.format(error=e), cs.Color.RED))
        # Only a bad repository root is about TARGET_REPO_PATH. The HTTP bind
        # refusal, an unknown workspace or a missing API key are ValueErrors
        # too, and the hint sent the user to a variable that was set (#2881).
        if isinstance(e, ex.RepoPathError) and not settings.QUIET:
            _mcp_server_notice(style(cs.CLI_MSG_HINT_TARGET_REPO, cs.Color.YELLOW))
        raise typer.Exit(1) from e
    except GraphUnavailableError as e:
        # Its message names the fix, so it is printed as is, as the app's
        # group prints it for every other command (#2443); but to stderr,
        # since stdout is the stdio transport's protocol stream (#2518).
        _mcp_server_notice(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e
    except Exception as e:
        _mcp_server_notice(style(cs.CLI_ERR_MCP_SERVER.format(error=e), cs.Color.RED))
        # Non-zero, so a host or supervisor can tell a server that never
        # came up from a clean shutdown (issue #2518).
        raise typer.Exit(1) from e


@app.command(
    name=ch.CLICommandName.GRAPH_LOADER,
    help=ch.CMD_GRAPH_LOADER,
    short_help=ch.CMD_GRAPH_LOADER,
    epilog=ch.EXAMPLES_GRAPH_LOADER,
    rich_help_panel=ch.PANEL_GRAPH,
)
def graph_loader_command(
    graph_file: str = typer.Argument(..., help=ch.HELP_GRAPH_FILE),
) -> None:
    from .graph_loader import load_graph

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        app_context.console.print(style(cs.CLI_MSG_GRAPH_SUMMARY, cs.Color.GREEN))
        app_context.console.print(f"  Total nodes: {summary['total_nodes']}")
        app_context.console.print(
            f"  Total relationships: {summary['total_relationships']}"
        )
        app_context.console.print(
            f"  Node types: {list(summary['node_labels'].keys())}"
        )
        app_context.console.print(
            f"  Relationship types: {list(summary['relationship_types'].keys())}"
        )
        app_context.console.print(
            f"  Exported at: {summary['metadata']['exported_at']}"
        )

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_LOAD_GRAPH.format(error=e), cs.Color.RED)
        )
        raise typer.Exit(1) from e


_DELEGATED_GROUP_CONTEXT = {
    "allow_extra_args": True,
    "allow_interspersed_args": False,
    "ignore_unknown_options": True,
}


def _run_delegated_group(group: click.Group, ctx: typer.Context) -> None:
    # The groups are the real click's and run non-standalone, so their usage
    # errors and aborts reach typer, and a typer that vendors click handles
    # only its own classes: they escaped as a traceback with exit 1. Reported
    # here the way click's standalone mode would, whichever typer is
    # installed (#2416).
    try:
        group.main(
            args=list(ctx.args),
            prog_name=ctx.command_path,
            standalone_mode=False,
        )
    except click.ClickException as e:
        e.show()
        raise typer.Exit(e.exit_code) from e
    except click.exceptions.Abort as e:
        typer.echo(cs.CLI_MSG_ABORTED, err=True)
        raise typer.Exit(1) from e


@app.command(
    name=ch.CLICommandName.LANGUAGE,
    help=ch.CMD_LANGUAGE,
    short_help=ch.CMD_LANGUAGE,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_MANAGE,
)
def language_command(ctx: typer.Context) -> None:
    _run_delegated_group(language_cli, ctx)


@app.command(
    name=ch.CLICommandName.DAEMON,
    help=ch.CMD_DAEMON,
    short_help=ch.CMD_DAEMON,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_MANAGE,
)
def daemon_command(ctx: typer.Context) -> None:
    _run_delegated_group(daemon_cli, ctx)


@app.command(
    name=ch.CLICommandName.WORKSPACE,
    help=ch.CMD_WORKSPACE,
    short_help=ch.CMD_WORKSPACE,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_MANAGE,
)
def workspace_command(ctx: typer.Context) -> None:
    _run_delegated_group(workspace_cli, ctx)


@app.command(
    name=ch.CLICommandName.TRACE,
    help=ch.CMD_TRACE,
    short_help=ch.CMD_TRACE,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def trace_command(ctx: typer.Context) -> None:
    _run_delegated_group(trace_cli, ctx)


@app.command(
    name=ch.CLICommandName.EDITS,
    help=ch.CMD_EDITS,
    short_help=ch.CMD_EDITS,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def edits_command(ctx: typer.Context) -> None:
    _run_delegated_group(edits_cli, ctx)


@app.command(
    name=ch.CLICommandName.GRAPH,
    help=ch.CMD_GRAPH,
    short_help=ch.CMD_GRAPH,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def graph_command(ctx: typer.Context) -> None:
    _run_delegated_group(graph_cli, ctx)


@app.command(
    name=ch.CLICommandName.PORT,
    help=ch.CMD_PORT,
    short_help=ch.CMD_PORT,
    add_help_option=False,
    context_settings=_DELEGATED_GROUP_CONTEXT,
    rich_help_panel=ch.PANEL_USE,
)
def port_command(ctx: typer.Context) -> None:
    _run_delegated_group(port_cli, ctx)


@app.command(
    name=ch.CLICommandName.CHECK,
    help=ch.CMD_CHECK,
    short_help=ch.CMD_CHECK,
    epilog=ch.EXAMPLES_CHECK,
    rich_help_panel=ch.PANEL_USE,
)
def check_command(
    base: str = typer.Option("HEAD", "--base", help=ch.HELP_CHECK_BASE),
    repo_path: Path = typer.Option(
        Path(cs.MCP_DEFAULT_DIRECTORY),
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_GRAPH_PROJECT
    ),
    fail_on_found: bool = typer.Option(
        False, "--fail-on-found", help=ch.HELP_CHECK_FAIL_ON_FOUND
    ),
    isolated: bool = typer.Option(False, "--isolated", help=ch.HELP_CHECK_ISOLATED),
) -> None:
    from .structural_check import CheckError, indexed_scope, run_check
    from .structural_delta import has_findings
    from .utils.path_utils import derive_project_name

    root = repo_path.resolve()
    name = project or derive_project_name(root)
    parsers, queries = load_parsers()
    with connect_memgraph(batch_size=settings.resolve_batch_size(None)) as ingestor:
        if name not in ingestor.list_projects():
            typer.echo(cs.CHECK_NOT_INDEXED.format(project=name), err=True)
            raise typer.Exit(code=1)
        try:
            exclude_paths, unignore_paths = indexed_scope(
                root, name, explicit=project is not None
            )
            delta = run_check(
                root,
                base,
                name,
                ingestor,
                parsers,
                queries,
                exclude_paths=exclude_paths,
                unignore_paths=unignore_paths,
                project_named=project is not None,
                isolated=isolated,
            )
        except CheckError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=1) from error
    typer.echo(json.dumps(delta, indent=cs.MCP_JSON_INDENT))
    if fail_on_found and has_findings(delta):
        raise typer.Exit(code=1)


EditReingest = Callable[[list[str]], ReingestReport]


@contextmanager
def _guarded_edit_reingest(
    ingestor: MemgraphIngestor, updater: "GraphUpdater", project_name: str, repo: Path
) -> Generator[tuple[EditReingest, "WriteMarker"]]:
    """The re-ingest an applied edit is measured through, behind the marker.

    Refuses before anything is planned when a run left the project's graph
    partial: the plan reads its call sites from the graph, so a caller a
    stopped sync deleted would be neither rewritten nor reported (Greptile
    review, PR #2164). The MCP tools refuse the same graph through
    `_guarded_rename_reingest`.

    The checkout's sync lock is held for the whole edit, from that check to
    the marker's clear: a sync taking the checkout between the check and the
    plan would leave the plan reading a graph it is rewriting (Greptile
    review, PR #3493). It waits behind a running sync, as a scoped re-ingest
    does, and the re-ingest re-enters it.

    The callback is `cgr watch`'s scoped re-ingest. The marker goes down from
    `before_write`, where the read-only prologue ends, and comes off once the
    re-ingest returned. A failure after the first write leaves it down, so
    the next edit, `cgr status` and the MCP server know the graph is partial;
    the edit's `graph_incomplete` alone would not outlive this process.
    """
    held = ExitStack()
    try:
        held.enter_context(repo_sync_lock(repo, project_name, wait=True))
    except SyncLockError as refused:
        typer.echo(str(refused), err=True)
        raise typer.Exit(code=1) from None
    with held:
        if (refusal := _partial_graph_refusal(ingestor, project_name)) is not None:
            typer.echo(refusal, err=True)
            raise typer.Exit(code=1)
        from .watch import WriteMarker

        with _sync_owner() as owner:
            marker = WriteMarker(ingestor, project_name, owner, None)

            def reingest(paths: list[str]) -> ReingestReport:
                report = updater.reingest(paths, before_write=marker.mark_scoped)
                if updater.reingest_mutated:
                    marker.clear()
                return report

            yield reingest, marker


def _mark_edit_partial(marker: "WriteMarker", project_name: str) -> str | None:
    """Mark the graph partial after an edit reported `graph_incomplete`.

    The edit's report decides, as in the MCP tools: a rollback whose
    re-ingest stopped before its first write put no marker down, yet left
    the graph describing files the rollback has since restored. Returns the
    error to print after the edit's JSON when the marker cannot go down, so
    the report of what changed is not lost.
    """
    try:
        marker.mark()
    except SyncRefusedError as refused:
        return cs.CLI_ERR_EDIT_MARKER_FAILED.format(
            project=project_name, error=refused.__cause__ or refused
        )
    return None


@app.command(
    name=ch.CLICommandName.RENAME,
    help=ch.CMD_RENAME,
    short_help=ch.CMD_RENAME,
    epilog=ch.EXAMPLES_RENAME,
    rich_help_panel=ch.PANEL_USE,
)
def rename_command(
    qualified_name: str = typer.Argument(..., help=ch.HELP_RENAME_QN),
    new_name: str = typer.Argument(..., help=ch.HELP_RENAME_NEW_NAME),
    repo_path: Path = typer.Option(
        Path(cs.MCP_DEFAULT_DIRECTORY),
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_GRAPH_PROJECT
    ),
    allow_heuristic: bool = typer.Option(
        False, cs.RENAME_CLI_ALLOW_HEURISTIC, help=ch.HELP_RENAME_ALLOW_HEURISTIC
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help=ch.HELP_RENAME_DRY_RUN),
) -> None:
    from .editing.rename import RenameRefused, rename, sites_for
    from .graph_cli import _project_and_fetch

    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:
        parsers, queries = load_parsers()
        from .graph_updater import GraphUpdater

        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_path.resolve(),
            parsers=parsers,
            queries=queries,
            project_name=name,
        )
        with _guarded_edit_reingest(ingestor, updater, name, repo_path.resolve()) as (
            reingest,
            marker,
        ):
            try:
                report = rename(
                    repo_path.resolve(),
                    fetch_all,
                    name,
                    qualified_name,
                    new_name,
                    allow_heuristic=allow_heuristic,
                    dry_run=dry_run,
                    reingest=reingest,
                    heuristic_opt_in=cs.RENAME_CLI_ALLOW_HEURISTIC,
                )
            except RenameRefused as refused:
                typer.echo(str(refused), err=True)
                for site in sites_for([*refused.ambiguous, *refused.unplanned]):
                    typer.echo(f"  {site}", err=True)
                for entry in refused.unlocatable:
                    typer.echo(f"  {entry}", err=True)
                raise typer.Exit(code=1) from refused
            marker_error = (
                _mark_edit_partial(marker, name) if report.graph_incomplete else None
            )
    payload = dict(report._asdict())
    payload[cs.KEY_SITES] = sites_for(report.sites)
    payload[cs.KEY_AMBIGUOUS] = sites_for(report.ambiguous)
    payload[cs.KEY_UNPLANNED] = sites_for(report.unplanned)
    payload["verdict"] = report.verdict._asdict() if report.verdict else None
    typer.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT, sort_keys=True))
    if marker_error is not None:
        typer.echo(marker_error, err=True)
        raise typer.Exit(code=1)
    if not report.applied and not dry_run:
        raise typer.Exit(code=1)


@app.command(
    name=ch.CLICommandName.CHANGE_SIGNATURE,
    help=ch.CMD_CHANGE_SIGNATURE,
    short_help=ch.CMD_CHANGE_SIGNATURE,
    epilog=ch.EXAMPLES_CHANGE_SIGNATURE,
    rich_help_panel=ch.PANEL_USE,
)
def change_signature_command(
    qualified_name: str = typer.Argument(..., help=ch.HELP_CHANGE_SIGNATURE_QN),
    new_params: list[str] = typer.Argument(..., help=ch.HELP_CHANGE_SIGNATURE_PARAMS),
    repo_path: Path = typer.Option(
        Path(cs.MCP_DEFAULT_DIRECTORY),
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
    project: str | None = typer.Option(None, "--project", help=ch.HELP_GRAPH_PROJECT),
    mapping: list[str] | None = typer.Option(
        None, "--map", help=ch.HELP_CHANGE_SIGNATURE_MAP
    ),
    allow_heuristic: bool = typer.Option(
        False, "--allow-heuristic", help=ch.HELP_CHANGE_SIGNATURE_ALLOW_HEURISTIC
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help=ch.HELP_CHANGE_SIGNATURE_DRY_RUN
    ),
) -> None:
    from .editing.signature import change_signature
    from .editing.signature_spec import (
        SignatureRefused,
        parse_mapping,
        sites_for,
        unmapped_for,
    )
    from .graph_cli import _project_and_fetch
    from .graph_query import source_root_for

    try:
        parsed = parse_mapping(mapping or [])
    except SignatureRefused as refused:
        typer.echo(str(refused), err=True)
        raise typer.Exit(code=1) from refused
    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:
        # An explicit --project may name a graph indexed from another
        # checkout; its repo-relative paths must not be edited under this
        # one. The MCP handler refuses the same mismatch.
        if source_root_for(fetch_all, name, repo_path) is None:
            typer.echo(
                cs.SIGNATURE_CLI_WRONG_ROOT.format(
                    project=name, root=repo_path.resolve()
                ),
                err=True,
            )
            raise typer.Exit(code=1)
        parsers, queries = load_parsers()
        from .graph_updater import GraphUpdater

        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_path.resolve(),
            parsers=parsers,
            queries=queries,
            project_name=name,
        )
        with _guarded_edit_reingest(ingestor, updater, name, repo_path.resolve()) as (
            reingest,
            marker,
        ):
            try:
                report = change_signature(
                    repo_path.resolve(),
                    fetch_all,
                    name,
                    qualified_name,
                    new_params,
                    parsed,
                    allow_heuristic=allow_heuristic,
                    dry_run=dry_run,
                    reingest=reingest,
                )
            except SignatureRefused as refused:
                typer.echo(str(refused), err=True)
                raise typer.Exit(code=1) from refused
            marker_error = (
                _mark_edit_partial(marker, name) if report.graph_incomplete else None
            )
    payload = dict(report._asdict())
    payload[cs.KEY_SITES] = sites_for(report.sites)
    payload[cs.KEY_UNMAPPED] = unmapped_for(report.unmapped)
    payload[cs.KEY_VERDICT] = report.verdict._asdict() if report.verdict else None
    typer.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT, sort_keys=True))
    if marker_error is not None:
        typer.echo(marker_error, err=True)
        raise typer.Exit(code=1)
    if not report.applied and not dry_run:
        raise typer.Exit(code=1)


@app.command(
    name=ch.CLICommandName.HELP,
    help=ch.CMD_HELP,
    short_help=ch.CMD_HELP,
    epilog=ch.EXAMPLES_HELP,
    rich_help_panel=ch.PANEL_HELP,
)
def help_command(
    ctx: typer.Context,
    command: list[str] | None = typer.Argument(None, help=ch.HELP_COMMAND),
) -> None:
    root_context = ctx.find_root()
    requested = command or []
    if not requested:
        typer.echo(root_context.get_help())
        return

    root_command = root_context.command
    command_name, *command_args = requested
    # Duck-typed, not isinstance(click.Group): a typer that vendors click
    # builds the app from a Group that is not the real click's (#1409).
    get_command = getattr(root_command, "get_command", None)
    if get_command is None:
        raise typer.Exit(1)

    target = get_command(root_context, command_name)
    if target is None:
        typer.echo(f"cgr: '{command_name}' is not a cgr command.", err=True)
        typer.echo("See 'cgr help'.", err=True)
        raise typer.Exit(2)

    try:
        target.main(
            args=[*command_args, "--help"],
            prog_name=f"{root_context.command_path} {command_name}",
            standalone_mode=False,
        )
    except _CLICK_EXCEPTIONS as error:
        error.show()
        raise typer.Exit(error.exit_code) from error


@app.command(
    name=ch.CLICommandName.STOP,
    help=ch.CMD_STOP,
    short_help=ch.CMD_STOP,
    rich_help_panel=ch.PANEL_MANAGE,
)
def stop_command() -> None:
    mgr = StackManager()
    try:
        mgr.down()
    except StackError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e
    _info(style("stack stopped", cs.Color.GREEN))


@app.command(
    name=ch.CLICommandName.STATUS,
    help=ch.CMD_STATUS,
    short_help=ch.CMD_STATUS,
    rich_help_panel=ch.PANEL_MANAGE,
)
def status_command() -> None:
    status = StackManager().status()
    app_context.console.print(
        f"stack:    {status.state.value} "
        f"(memgraph={status.memgraph_endpoint} reachable={status.memgraph_reachable}, "
        f"qdrant={status.qdrant_endpoint} reachable={status.qdrant_reachable})"
    )
    app_context.console.print(f"compose:  {status.compose_file}")
    syncs = _project_syncs() if status.memgraph_reachable else None
    if syncs is None:
        app_context.console.print(
            cs.CLI_STATUS_SYNCS_NEED_GRAPH.format(endpoint=status.memgraph_endpoint)
        )
        return
    if not syncs:
        app_context.console.print(
            cs.CLI_STATUS_SYNCS_NONE.format(endpoint=status.memgraph_endpoint)
        )
        return
    app_context.console.print(cs.CLI_STATUS_SYNCS_HEADER)
    for sync in syncs:
        app_context.console.print(_sync_row(sync))


def _stamp_text(stamp: ResultValue | None) -> str | None:
    """A recorded sync time as `cgr status` prints it; None when never stamped."""
    return str(stamp) if stamp else None


def _project_syncs() -> list[ProjectSync] | None:
    """Every project the connected graph holds, with its last sync (#2444).

    The graph is the only source: a project deleted or wiped goes with its
    node, and one synced into another Memgraph is not in this one. A project
    whose first sync never finished has only its `:IncompleteRun` marker
    (#2219), so it is listed from that. None when the graph cannot be read:
    status must still print without it.
    """
    try:
        with connect_memgraph(1) as ingestor:
            projects = ingestor.fetch_all(cq.CYPHER_PROJECT_SYNC_TIMES)
            markers = ingestor.fetch_all(cq.CYPHER_PROJECTS_WITH_INCOMPLETE_RUNS)
    except Exception as exc:
        logger.warning(ls.CLI_SYNC_STATE_UNREADABLE.format(error=exc))
        return None
    synced_at = {
        str(row[cs.KEY_NAME]): row.get(cs.KEY_LAST_SYNCED_AT)
        for row in projects
        if row.get(cs.KEY_NAME)
    }
    interrupted = {str(row["project"]) for row in markers if row.get("project")}
    return [
        ProjectSync(
            name=name,
            last_synced_at=_stamp_text(synced_at.get(name)),
            interrupted=name in interrupted,
        )
        for name in sorted(synced_at.keys() | interrupted)
    ]


def _sync_row(sync: ProjectSync) -> str:
    details: list[str] = []
    if sync.last_synced_at:
        details.append(cs.CLI_STATUS_SYNC_TIME.format(time=sync.last_synced_at))
    elif not sync.interrupted:
        details.append(cs.CLI_STATUS_SYNC_NOT_RECORDED)
    if sync.interrupted:
        details.append(
            cs.CLI_STATUS_SYNC_MARKED.format(marker=cs.CLI_STATUS_SYNC_INCOMPLETE)
        )
    return cs.CLI_STATUS_SYNC_ROW.format(project=sync.name, detail=" ".join(details))


@app.command(
    name=ch.CLICommandName.DOCTOR,
    help=ch.CMD_DOCTOR,
    short_help=ch.CMD_DOCTOR,
    rich_help_panel=ch.PANEL_MANAGE,
)
def doctor() -> None:
    checker = HealthChecker()
    results = checker.run_all_checks()

    passed, total = checker.get_summary()

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="cyan", no_wrap=False)

    encoding = app_context.console.encoding
    for result in results:
        status = status_mark(result.passed, encoding)
        status_color = cs.Color.GREEN if result.passed else cs.Color.RED
        status_text = style(status, status_color, cs.StyleModifier.NONE)

        check_name = f"{status_text} {result.name}"
        table.add_row(check_name)

    panel = Panel(
        table,
        title="Health Check",
        border_style="dim",
        padding=(1, 2),
    )

    app_context.console.print(panel)

    app_context.console.print()
    summary_text = f"{passed}/{total} checks passed"
    if passed == total:
        app_context.console.print(style(summary_text, cs.Color.GREEN))
    else:
        app_context.console.print(style(summary_text, cs.Color.YELLOW))

    failed_checks = [r for r in results if not r.passed and r.error]
    if failed_checks:
        app_context.console.print()
        app_context.console.print(style("Failed checks details:", cs.Color.YELLOW))
        for result in failed_checks:
            error_msg = f"  {result.name}: {result.error}"
            app_context.console.print(
                style(error_msg, cs.Color.YELLOW, cs.StyleModifier.NONE)
            )

    if passed < total:
        raise typer.Exit(1)


def _node_label(row: ResultRow) -> str:
    labels = row.get("labels")
    # A row with no label list (absent, or null from the engine) is shown as
    # unknown instead of failing the whole table.
    if not isinstance(labels, list):
        return cs.CLI_STATS_UNKNOWN
    return ":".join(str(label) for label in labels) or cs.CLI_STATS_UNKNOWN


def _build_stats_table(
    title: str,
    col_label: str,
    rows: list[ResultRow],
    get_label: Callable[[ResultRow], str],
    total_label: str,
) -> Table:
    table = Table(
        title=style(title, cs.Color.GREEN),
        show_header=True,
        header_style=f"{cs.StyleModifier.BOLD} {cs.Color.MAGENTA}",
    )
    table.add_column(col_label, style=cs.Color.CYAN)
    table.add_column(cs.CLI_STATS_COL_COUNT, style=cs.Color.YELLOW, justify="right")
    total = 0
    for row in rows:
        raw_count = row.get("count", 0)
        count = int(raw_count) if isinstance(raw_count, int | float) else 0
        total += count
        table.add_row(get_label(row), f"{count:,}")
    table.add_section()
    table.add_row(
        style(total_label, cs.Color.GREEN),
        style(f"{total:,}", cs.Color.GREEN),
    )
    return table


@app.command(
    name=ch.CLICommandName.CONTEXT,
    help=ch.CMD_CONTEXT,
    short_help=ch.CMD_CONTEXT,
    epilog=ch.EXAMPLES_CONTEXT,
    rich_help_panel=ch.PANEL_USE,
)
def context_command(
    target: str = typer.Argument(..., help=ch.HELP_CONTEXT_TARGET),
    budget: int = typer.Option(
        cs.CONTEXT_DEFAULT_BUDGET, "--budget", min=1, help=ch.HELP_CONTEXT_BUDGET
    ),
    repo_path: Path = typer.Option(
        Path(cs.MCP_DEFAULT_DIRECTORY),
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_GRAPH_REPO_PATH,
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_GRAPH_PROJECT
    ),
) -> None:
    from . import graph_query
    from .context_slice import context as build_context
    from .graph_cli import _project_and_fetch
    from .tools.semantic_search import semantic_code_search

    name, fetch_all, ingestor = _project_and_fetch(project, repo_path)
    with ingestor:  # type: ignore[attr-defined]
        payload = build_context(
            fetch_all,
            name,
            target,
            budget,
            graph_query.source_root_for(fetch_all, name, repo_path),
            search=lambda text: semantic_code_search(ingestor, text, project=name),  # type: ignore[arg-type]
        )
    typer.echo(json.dumps(payload, indent=cs.MCP_JSON_INDENT))
    if payload["resolved"] is None:
        typer.echo(cs.CONTEXT_UNRESOLVED.format(target=target), err=True)
        raise typer.Exit(code=1)
    # Only the target's own trim marks a slice truncated, so truncated and
    # empty means not even its first line fitted.
    if payload["truncated"] and not payload["pieces"]:
        typer.echo(
            cs.CONTEXT_TARGET_OVER_BUDGET.format(
                target=payload["resolved"], budget=budget
            ),
            err=True,
        )
        raise typer.Exit(code=1)


@app.command(
    name=ch.CLICommandName.STATS,
    help=ch.CMD_STATS,
    short_help=ch.CMD_STATS,
    epilog=ch.EXAMPLES_STATS,
    rich_help_panel=ch.PANEL_GRAPH,
)
def stats(
    project: list[str] = typer.Option(
        [], "--project", "-p", help=ch.HELP_STATS_PROJECT
    ),
    # The spelling before issue #2415, kept working for scripts.
    project_name: list[str] = typer.Option(
        [], cs.CLI_OPT_PROJECT_NAME, "-n", hidden=True
    ),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        exists=True,
        file_okay=False,
        help=ch.HELP_STATS_REPO_PATH,
    ),
    workspace: str | None = typer.Option(
        None, "--workspace", help=ch.HELP_STATS_WORKSPACE
    ),
) -> None:
    workspace_config = _load_workspace_or_exit(workspace)
    names = [*project, *project_name]
    requested = _requested_projects(names, workspace_config, repo_path)
    # An empty list is what "no scope" looks like to the queries below, so a
    # scope that was asked for and came out empty would report the whole
    # shared graph as if it were that scope's (review of PR 2436).
    if not requested and (names or workspace_config is not None):
        app_context.console.print(
            style(
                cs.CLI_ERR_STATS_EMPTY_WORKSPACE.format(name=workspace)
                if workspace_config is not None
                else cs.CLI_ERR_STATS_EMPTY_PROJECT_NAME,
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)

    app_context.console.print(style(cs.CLI_MSG_CONNECTING_STATS, cs.Color.CYAN))

    refusal = ""
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = list(ingestor.list_projects())
            if missing := [name for name in requested if name not in projects]:
                refusal = scope_refusal(missing, projects, repo_path, ingestor)
                node_results, rel_results, breakdown = [], [], []
            else:
                node_results, rel_results, breakdown = _stats_rows(
                    ingestor, requested, len(projects)
                )

    except GraphUnavailableError:
        raise
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_STATS_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.STATS_ERROR.format(error=e))
        raise typer.Exit(1) from e

    # Outside the connection, as dead-code does: a typo must not look like a
    # failed query to the service layer's error logging.
    if refusal:
        app_context.console.print(style(refusal, cs.Color.RED))
        raise typer.Exit(1)
    _print_stats(requested, node_results, rel_results, breakdown)


def _stats_rows(
    ingestor: MemgraphIngestor, requested: list[str], project_count: int
) -> tuple[list[ResultRow], list[ResultRow], list[ResultRow]]:
    """Node rows, relationship rows and the per-project breakdown.

    Scoped counts cover what the named projects own; unscoped ones keep the
    whole-graph totals and, when they span several projects, a breakdown that
    says which project contributed what.
    """
    if requested:
        params = {cs.KEY_PROJECT_NAMES: requested}
        return (
            ingestor.fetch_all(cq.CYPHER_STATS_PROJECT_NODE_COUNTS, params),
            ingestor.fetch_all(cq.CYPHER_STATS_PROJECT_RELATIONSHIP_COUNTS, params),
            [],
        )
    return (
        ingestor.fetch_all(cq.CYPHER_STATS_NODE_COUNTS),
        ingestor.fetch_all(cq.CYPHER_STATS_RELATIONSHIP_COUNTS),
        ingestor.fetch_all(cq.CYPHER_STATS_PER_PROJECT) if project_count > 1 else [],
    )


def _print_stats(
    requested: list[str],
    node_results: list[ResultRow],
    rel_results: list[ResultRow],
    breakdown: list[ResultRow],
) -> None:
    console = app_context.console
    if requested:
        console.print(
            style(
                cs.CLI_STATS_SCOPE.format(projects=", ".join(requested)), cs.Color.CYAN
            )
        )
    console.print(
        _build_stats_table(
            cs.CLI_STATS_NODE_TITLE,
            cs.CLI_STATS_COL_NODE_TYPE,
            node_results,
            _node_label,
            cs.CLI_STATS_TOTAL_NODES,
        )
    )
    console.print()
    console.print(
        _build_stats_table(
            cs.CLI_STATS_REL_TITLE,
            cs.CLI_STATS_COL_REL_TYPE,
            rel_results,
            lambda r: str(r.get("type", cs.CLI_STATS_UNKNOWN)),
            cs.CLI_STATS_TOTAL_RELS,
        )
    )
    if breakdown:
        console.print()
        console.print(style(cs.CLI_STATS_PER_PROJECT_TITLE, cs.Color.GREEN))
        for row in breakdown:
            console.print(
                cs.CLI_STATS_PER_PROJECT_ROW.format(
                    project=row.get(cs.KEY_PROJECT),
                    nodes=_int(row.get(cs.KEY_NODES)),
                    relationships=_int(row.get(cs.KEY_RELATIONSHIPS)),
                ),
                markup=False,
                highlight=False,
            )


def _int(value: ResultValue | None) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _one_project(project: str | None, alias: str | None, alias_flag: str) -> str | None:
    """`--project`, or the old spelling it replaced (issue #2415).

    Both naming different projects is refused before the graph is read,
    rather than one of them silently winning.
    """
    if project is None:
        return alias
    if alias is not None and alias.strip() != project.strip():
        _exit_with_error(
            cs.CLI_ERR_PROJECT_GIVEN_TWICE.format(
                project=project, alias=alias_flag, other=alias
            )
        )
    return project


def _scan_project_option(project: str | None, alias: str | None) -> str | None:
    # `-p ""` read as no name, as `-n ""` always did for these scans.
    chosen = _one_project(project, alias, cs.CLI_OPT_PROJECT_NAME)
    return chosen.strip() if chosen else None


def _require_project(choice: ProjectChoice) -> str:
    # Raised OUTSIDE the connection context so a user typo never trips the
    # service layer's error logging on exit, and before anything is reported:
    # a name absent from the graph must error, not scan a nonexistent prefix
    # and report a clean project.
    if choice.name is None:
        _exit_with_error(choice.refusal)
    return choice.name


def _dead_code_config(
    include_tests: bool,
    include_classes: bool,
    entry_points: list[str],
    decorator_roots: list[str],
    min_resolution: cs.EdgeResolution | None = None,
    endpoint_roots: bool = True,
    public_roots: bool = True,
) -> DeadCodeConfig:
    from .dead_code import normalize_decorator_root

    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(
            {d.lower() for d in cs.DEFAULT_ROOT_DECORATORS}
            | {normalize_decorator_root(d) for d in decorator_roots}
        ),
        entry_points=tuple(entry_points),
        min_resolution=str(min_resolution) if min_resolution is not None else None,
        endpoint_roots=endpoint_roots,
        public_roots=public_roots,
    )


def _filter_excluded_rows(rows: list[ResultRow], exclude: list[str]) -> list[ResultRow]:
    # Drop candidates whose file path matches an exclude glob (generated dirs
    # like client/core or *.gen.* have no in-repo caller, so every symbol reports
    # as dead). fnmatch's '*' spans '/', so '*client/core*' matches at any depth.
    if not exclude:
        return rows
    return [
        row
        for row in rows
        if not any(
            fnmatch(str(row.get(cs.KEY_PATH) or ""), pattern) for pattern in exclude
        )
    ]


def _to_dead_code_row(row: ResultRow) -> DeadCodeRow:
    start = row.get(cs.KEY_START_LINE, 0)
    end = row.get(cs.KEY_END_LINE, 0)
    return DeadCodeRow(
        label=str(row.get(cs.KEY_LABEL, "")),
        name=str(row.get(cs.KEY_NAME, "")),
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        path=str(row.get(cs.KEY_PATH) or ""),
        start_line=int(start) if isinstance(start, int | float) else 0,
        end_line=int(end) if isinstance(end, int | float) else 0,
    )


def _width_set_by_user() -> bool:
    # Rich reads $COLUMNS only when it is a number, so only that counts.
    return os.environ.get(cs.ENV_COLUMNS, "").isdigit()


def _print_report_table(console: Console, table: Table) -> None:
    """Print a report table at its content's width when no screen shows it.

    A file or pipe has no width to fit, yet Rich lays the table out at
    $COLUMNS or 80 columns there and cuts every long name with an ellipsis,
    so a CI report's rows read alike and depended on who ran it (issue
    #2561). A terminal keeps its own width, and a $COLUMNS the user set still
    wins. The stream's own isatty decides, not Rich's is_terminal: a forced
    terminal or FORCE_COLOR chooses styling, not whether a screen clips lines.
    The table is sized in place.
    """
    if console.file.isatty() or _width_set_by_user():
        console.print(table)
        return
    unbounded = console.options.update_width(sys.maxsize)
    table.width = console.measure(table, options=unbounded).maximum
    # Wider than the console on purpose, so it must not be cropped back to it.
    console.print(table, crop=False)


def _build_dead_code_table(candidates: list[DeadCodeRow], project_name: str) -> Table:
    table = Table(
        title=style(
            cs.CLI_DEADCODE_TABLE_TITLE.format(project_name=project_name),
            cs.Color.GREEN,
        ),
        show_header=True,
        header_style=f"{cs.StyleModifier.BOLD} {cs.Color.MAGENTA}",
    )
    table.add_column(cs.CLI_DEADCODE_COL_KIND, style=cs.Color.MAGENTA)
    # Folded, not ellipsised: a qualified name has no space to wrap at, so a
    # narrow terminal cut every row back to the same package prefix and never
    # named the method (issue #2561).
    table.add_column(
        cs.CLI_DEADCODE_COL_QUALIFIED_NAME, style=cs.Color.CYAN, overflow="fold"
    )
    # Lines mean nothing without the file they are in (issue #2561).
    table.add_column(cs.CLI_DEADCODE_COL_PATH, style=cs.Color.YELLOW, overflow="fold")
    table.add_column(cs.CLI_DEADCODE_COL_LINES, style=cs.Color.YELLOW, justify="right")
    # A str cell is parsed as markup, and a route directory such as
    # `app/[slug]/` reads as a tag in both the path and the qn derived from it.
    for row in candidates:
        table.add_row(
            row["label"],
            Text(row["qualified_name"]),
            Text(row["path"]),
            cs.CLI_DEADCODE_LINE_RANGE.format(
                start=row["start_line"], end=row["end_line"]
            ),
        )
    return table


def _print_root_rule(console: Console, public_roots: bool) -> None:
    # A clean report says which root rule produced it, on screen and in a
    # saved copy alike: with public roots on, it can just mean every function
    # is public (issue #2527).
    console.print(
        cs.CLI_DEADCODE_RULE_PUBLIC_ROOTS
        if public_roots
        else cs.CLI_DEADCODE_RULE_NO_PUBLIC_ROOTS,
        markup=False,
        highlight=False,
        soft_wrap=True,
    )


def _save_dead_code_table(
    output: Path,
    table: Table,
    notice: str,
    public_roots: bool,
    has_candidates: bool,
) -> None:
    with output.open("w", encoding=cs.ENCODING_UTF8) as fh:
        file_console = Console(file=fh)
        _print_report_table(file_console, table)
        if not has_candidates:
            _print_root_rule(file_console, public_roots)
        if notice:
            file_console.print(notice)


def _emit_dead_code(
    candidates: list[DeadCodeRow],
    output_format: cs.DeadCodeFormat,
    output: Path | None,
    project_name: str,
    structural_tier_symbols: int = 0,
    public_roots: bool = True,
) -> None:
    if output_format == cs.DeadCodeFormat.JSON:
        payload = json.dumps(candidates, indent=2)
        if output is not None:
            output.write_text(payload, encoding=cs.ENCODING_UTF8)
            app_context.console.print(
                style(
                    cs.CLI_DEADCODE_WRITTEN.format(count=len(candidates), path=output),
                    cs.Color.GREEN,
                )
            )
            return
        typer.echo(payload)
        return

    # The coverage notice follows the table into whichever sink it goes, so a
    # saved report (CI artifact, shared review) never reads as "all clean"
    # when whole languages went unanalyzed.
    notice = (
        cs.CLI_DEADCODE_STRUCTURAL_TIER_SKIPPED.format(count=structural_tier_symbols)
        if structural_tier_symbols
        else ""
    )
    table = _build_dead_code_table(candidates, project_name)
    if output is not None:
        _save_dead_code_table(output, table, notice, public_roots, bool(candidates))
        app_context.console.print(
            style(
                cs.CLI_DEADCODE_WRITTEN.format(count=len(candidates), path=output),
                cs.Color.GREEN,
            )
        )
        if not candidates:
            _print_root_rule(app_context.console, public_roots)
        if notice:
            app_context.console.print(style(notice, cs.Color.YELLOW))
        return

    if not candidates:
        app_context.console.print(style(cs.CLI_DEADCODE_NONE, cs.Color.GREEN))
        _print_root_rule(app_context.console, public_roots)
    else:
        _print_report_table(app_context.console, table)
        app_context.console.print(
            style(cs.CLI_DEADCODE_SUMMARY.format(count=len(candidates)), cs.Color.GREEN)
        )
    if notice:
        app_context.console.print(style(notice, cs.Color.YELLOW))


def _notice_single_project_endpoint_roots(show_progress: bool) -> None:
    """Endpoint roots off on a graph holding one project: every endpoint
    reads as uncalled. Stdout carries the JSON payload when that format is
    chosen (local review P1), and only the table under --quiet, so the
    notice goes to stderr then."""
    if show_progress:
        app_context.console.print(
            style(cs.CLI_DEADCODE_SINGLE_PROJECT_ENDPOINTS, cs.Color.YELLOW)
        )
    else:
        typer.echo(cs.CLI_DEADCODE_SINGLE_PROJECT_ENDPOINTS, err=True)


def _refuse_without_endpoint_data(
    error: ex.EndpointDataMissingError, show_progress: bool
) -> NoReturn:
    """No verdict, rather than one that reads as nothing unreachable. A
    machine format owns stdout, so the refusal goes to stderr there, as the
    single-project notice does."""
    if show_progress:
        app_context.console.print(style(str(error), cs.Color.RED))
    else:
        typer.echo(str(error), err=True)
    raise typer.Exit(1)


@app.command(
    name=ch.CLICommandName.DEAD_CODE,
    help=ch.DESC_DEAD_CODE,
    short_help=ch.CMD_DEAD_CODE,
    epilog=ch.EXAMPLES_DEAD_CODE,
    rich_help_panel=ch.PANEL_GRAPH,
)
# Typer turns each parameter into one flag of the command, so the count is the
# command's option list, not a call site to simplify (python:S107 accepted).
def dead_code(
    project: str | None = typer.Option(  # NOSONAR
        None, "--project", "-p", help=ch.HELP_SCAN_PROJECT
    ),
    # The spelling before issue #2415, kept working for scripts.
    project_name: str | None = typer.Option(
        None, cs.CLI_OPT_PROJECT_NAME, "-n", hidden=True
    ),
    repo_path: Path | None = typer.Option(
        None, "--repo-path", exists=True, file_okay=False, help=ch.HELP_SCAN_REPO_PATH
    ),
    entry_point: list[str] = typer.Option(
        [], "--entry-point", "-e", help=ch.HELP_DEADCODE_ENTRY_POINT
    ),
    decorator_root: list[str] = typer.Option(
        [], "--decorator-root", help=ch.HELP_DEADCODE_DECORATOR_ROOT
    ),
    exclude: list[str] = typer.Option([], "--exclude", help=ch.HELP_DEADCODE_EXCLUDE),
    include_tests: bool = typer.Option(
        True,
        "--include-tests/--no-include-tests",
        help=ch.HELP_DEADCODE_INCLUDE_TESTS,
    ),
    include_classes: bool = typer.Option(
        False,
        "--classes/--no-classes",
        help=ch.HELP_DEADCODE_CLASSES,
    ),
    output_format: cs.DeadCodeFormat = typer.Option(
        cs.DeadCodeFormat.TABLE, "--format", help=ch.HELP_DEADCODE_FORMAT
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help=ch.HELP_DEADCODE_OUTPUT
    ),
    fail_on_found: bool = typer.Option(
        False, "--fail-on-found", help=ch.HELP_DEADCODE_FAIL_ON_FOUND
    ),
    min_resolution: cs.EdgeResolution | None = typer.Option(
        None, "--min-resolution", help=ch.HELP_DEADCODE_MIN_RESOLUTION
    ),
    endpoint_roots: bool = typer.Option(
        True,
        "--endpoint-roots/--no-endpoint-roots",
        help=ch.HELP_DEADCODE_ENDPOINT_ROOTS,
    ),
    public_roots: bool = typer.Option(
        True,
        "--public-roots/--no-public-roots",
        help=ch.HELP_DEADCODE_PUBLIC_ROOTS,
    ),
) -> None:
    from .dead_code import collect_dead_code_with_coverage

    chosen = _scan_project_option(project, project_name)
    show_progress = (
        output_format == cs.DeadCodeFormat.TABLE
        and output is None
        and not settings.QUIET
    )
    if show_progress:
        app_context.console.print(style(cs.CLI_DEADCODE_CONNECTING, cs.Color.CYAN))

    choice = ProjectChoice(None)
    rows: list[ResultRow] = []
    structural_tier_symbols = 0
    endpoint_gap: ex.EndpointDataMissingError | None = None
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = ingestor.list_projects()
            choice = choose_project(chosen, repo_path, projects, ingestor)
            if (resolved := choice.name) is not None:
                logger.info(ls.DEADCODE_SCANNING.format(project_name=resolved))
                try:
                    rows, structural_tier_symbols = collect_dead_code_with_coverage(
                        ingestor,
                        resolved,
                        _dead_code_config(
                            include_tests,
                            include_classes,
                            entry_point,
                            decorator_root,
                            min_resolution,
                            endpoint_roots,
                            public_roots,
                        ),
                    )
                except ex.EndpointDataMissingError as e:
                    # Held until the connection closes: a finding about the
                    # project, which the service layer would log on exit as a
                    # failed session with a traceback (issue #2896).
                    endpoint_gap = e
                if not endpoint_roots and len(projects) <= 1 and not endpoint_gap:
                    _notice_single_project_endpoint_roots(show_progress)
    except GraphUnavailableError:
        raise
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_DEADCODE_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.DEADCODE_ERROR.format(error=e))
        raise typer.Exit(1) from e

    if endpoint_gap is not None:
        _refuse_without_endpoint_data(endpoint_gap, show_progress)
    resolved = _require_project(choice)

    candidates = [
        _to_dead_code_row(row) for row in _filter_excluded_rows(rows, exclude)
    ]
    _emit_dead_code(
        candidates,
        output_format,
        output,
        resolved,
        structural_tier_symbols,
        public_roots,
    )

    if fail_on_found and candidates:
        raise typer.Exit(1)


def _similarity_text(group: DuplicateGroup) -> str:
    if group["kind"] == cs.KIND_EXACT:
        return cs.CLI_DUPLICATES_SIMILARITY_EXACT
    low, high = group["similarity"] * 100, group["max_similarity"] * 100
    single = cs.CLI_DUPLICATES_SIMILARITY_PCT.format(pct=low)
    if single == cs.CLI_DUPLICATES_SIMILARITY_PCT.format(pct=high):
        return single
    # A cluster's members are linked through each other (issue #2473), so
    # one score would hide how far apart its weakest and strongest links are.
    return cs.CLI_DUPLICATES_SIMILARITY_RANGE.format(low=low, high=high)


def _exact_copy_numbers(group: DuplicateGroup) -> dict[str, str]:
    """Qualified name -> number of the exact-copy set it belongs to.

    Only a `similar` group nests exact copies; an `exact` group is one set
    as a whole, so its rows stay unnumbered.
    """
    if group["kind"] == cs.KIND_EXACT:
        return {}
    return {
        qualified_name: str(number)
        for number, names in enumerate(group["exact_subgroups"], start=1)
        for qualified_name in names
    }


def _exact_copy_cells(
    numbers: dict[str, str], member: DuplicateMember, shown: bool
) -> list[str]:
    return [numbers.get(member["qualified_name"], "")] if shown else []


def _duplicates_location_cell(
    member: DuplicateMember, root_path: Path | None
) -> Text | str:
    """Location as an OSC 8 hyperlink into the editor, plain when rootless.

    Rich drops hyperlinks on non-terminal sinks, so file/JSON outputs are
    unaffected; graphs indexed before Project.root_path existed fall back
    to plain text.
    """
    location = cs.CLI_DUPLICATES_LOCATION.format(
        path=member["path"],
        start=member["start_line"],
        end=member["end_line"],
    )
    if root_path is None:
        return location
    url = editor_url(root_path / member["path"], member["start_line"])
    if url is None:
        return location
    cell = Text(location)
    cell.stylize(cs.STYLE_LINK.format(url=url))
    return cell


def _duplicates_group_cell(
    number: int, group: DuplicateGroup, root_path: Path | None
) -> Text | str:
    """Group number as a diff:// hyperlink opening the first two members
    side by side in a terminal that understands the scheme (Croft); plain
    text when rootless or links are off."""
    label = str(number)
    if root_path is None:
        return label
    first, second = group["members"][0], group["members"][1]
    url = diff_link(
        root_path / first["path"],
        first["start_line"],
        root_path / second["path"],
        second["start_line"],
    )
    if url is None:
        return label
    cell = Text(label)
    cell.stylize(cs.STYLE_LINK.format(url=url))
    return cell


def _build_duplicates_table(
    groups: list[DuplicateGroup], project_name: str, root_path: Path | None = None
) -> Table:
    table = Table(
        title=style(
            cs.CLI_DUPLICATES_TABLE_TITLE.format(project_name=project_name),
            cs.Color.GREEN,
        ),
        show_header=True,
        header_style=f"{cs.StyleModifier.BOLD} {cs.Color.MAGENTA}",
    )
    table.add_column(cs.CLI_DUPLICATES_COL_GROUP, style=cs.Color.MAGENTA)
    table.add_column(cs.CLI_DUPLICATES_COL_KIND, style=cs.Color.MAGENTA)
    table.add_column(
        cs.CLI_DUPLICATES_COL_SIMILARITY, style=cs.Color.YELLOW, justify="right"
    )
    # Folded, not ellipsised: a qualified name or path has no space to wrap
    # at, so at pipe width every cell ended in "…" and never named the
    # function (issue #2397).
    table.add_column(cs.CLI_DUPLICATES_COL_MEMBER, style=cs.Color.CYAN, overflow="fold")
    table.add_column(
        cs.CLI_DUPLICATES_COL_LOCATION, style=cs.Color.YELLOW, overflow="fold"
    )
    copy_numbers = [_exact_copy_numbers(group) for group in groups]
    # Added only when a cluster holds exact copies, so every other report
    # keeps the width it had.
    show_copies = any(copy_numbers)
    if show_copies:
        table.add_column(cs.CLI_DUPLICATES_COL_EXACT, style=cs.Color.MAGENTA)
    # The title names the project, so each member drops that prefix: at pipe
    # width the column otherwise showed nothing else (issue #2397). Only the
    # dotted prefix goes, so `projx.mod` under `proj` keeps its name.
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    for number, group in enumerate(groups, start=1):
        for at, member in enumerate(group["members"]):
            table.add_row(
                _duplicates_group_cell(number, group, root_path) if at == 0 else "",
                group["kind"] if at == 0 else "",
                _similarity_text(group) if at == 0 else "",
                member["qualified_name"].removeprefix(prefix),
                _duplicates_location_cell(member, root_path),
                *_exact_copy_cells(copy_numbers[number - 1], member, show_copies),
            )
        table.add_section()
    return table


def _emit_duplicates_json(
    groups: list[DuplicateGroup],
    output: Path | None,
    skipped_symbols: int,
    truncated: bool,
) -> None:
    from .duplicates import reported_groups

    # Only this report prints links, so only it expands them to member pairs.
    reported, links_truncated = reported_groups(groups, cs.DUPLICATES_MAX_GROUP_LINKS)
    # Envelope, not a bare list: scan-completeness metadata must reach
    # JSON consumers too, or a CI artifact reads as a complete scan when
    # symbols went unanalyzed or group enumeration hit its cap.
    payload = json.dumps(
        {
            cs.KEY_DUPLICATE_GROUPS: reported,
            cs.KEY_SKIPPED_SYMBOLS: skipped_symbols,
            cs.KEY_TRUNCATED: truncated or links_truncated,
        },
        indent=2,
    )
    if output is None:
        typer.echo(payload)
        return
    output.write_text(payload, encoding=cs.ENCODING_UTF8)
    _print_duplicates_written(len(groups), output)


def _duplicates_notices(
    skipped_symbols: int, truncated: bool, all_skipped: bool
) -> list[str]:
    # As with dead-code, the completeness notices follow the report into its
    # sink so a saved artifact never reads as "all clean" when symbols went
    # unanalyzed or group enumeration hit its cap.
    notices = []
    if all_skipped:
        notices.append(cs.CLI_DUPLICATES_STALE_GRAPH.format(count=skipped_symbols))
    elif skipped_symbols:
        notices.append(
            cs.CLI_DUPLICATES_STRUCTURAL_TIER_SKIPPED.format(count=skipped_symbols)
        )
    if truncated:
        notices.append(cs.CLI_DUPLICATES_TRUNCATED_NOTICE)
    return notices


def _print_duplicates_written(count: int, output: Path) -> None:
    app_context.console.print(
        style(
            cs.CLI_DUPLICATES_WRITTEN.format(count=count, path=output),
            cs.Color.GREEN,
        )
    )


def _write_duplicates_file(
    table: Table, notices: list[str], group_count: int, output: Path
) -> None:
    with output.open("w", encoding=cs.ENCODING_UTF8) as fh:
        file_console = Console(file=fh)
        _print_report_table(file_console, table)
        for notice in notices:
            file_console.print(notice)
    _print_duplicates_written(group_count, output)


def _emit_duplicates(
    groups: list[DuplicateGroup],
    output_format: cs.DuplicatesFormat,
    output: Path | None,
    project_name: str,
    skipped_symbols: int = 0,
    truncated: bool = False,
    analyzed_symbols: int = 0,
    root_path: Path | None = None,
) -> None:
    if output_format == cs.DuplicatesFormat.JSON:
        _emit_duplicates_json(groups, output, skipped_symbols, truncated)
        return

    # Every symbol skipped and none analyzed: the graph was indexed before
    # fingerprint stamping, so "no duplicates" would be vacuous and the
    # pattern-tier wording a misdiagnosis - recommend a re-index instead.
    all_skipped = skipped_symbols > 0 and analyzed_symbols == 0
    notices = _duplicates_notices(skipped_symbols, truncated, all_skipped)
    # A broken CGR_EDITOR_URL_TEMPLATE degrades to plain locations; the
    # notice says so instead of the template error aborting mid-table.
    if root_path is not None and (problem := url_template_problem()) is not None:
        notices.append(problem)
    table = _build_duplicates_table(groups, project_name, root_path)
    if output is not None:
        _write_duplicates_file(table, notices, len(groups), output)
        for notice in notices:
            app_context.console.print(style(notice, cs.Color.YELLOW))
        return

    if not groups:
        if not all_skipped:
            app_context.console.print(style(cs.CLI_DUPLICATES_NONE, cs.Color.GREEN))
    else:
        _print_report_table(app_context.console, table)
        members = sum(len(group["members"]) for group in groups)
        app_context.console.print(
            style(
                cs.CLI_DUPLICATES_SUMMARY.format(groups=len(groups), members=members),
                cs.Color.GREEN,
            )
        )
    for notice in notices:
        app_context.console.print(style(notice, cs.Color.YELLOW))


def _open_duplicate_group(
    groups: list[DuplicateGroup],
    number: int,
    root_path: Path | None,
    project_name: str,
) -> None:
    """Open a group's first two members side by side in the user's editor."""
    if number > len(groups):
        app_context.console.print(
            style(
                cs.CLI_ERR_DUPLICATES_OPEN_UNKNOWN_GROUP.format(
                    number=number, count=len(groups)
                ),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)
    if root_path is None:
        app_context.console.print(
            style(
                cs.CLI_ERR_DUPLICATES_OPEN_NO_ROOT.format(project=project_name),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)
    members = groups[number - 1]["members"]
    left = root_path / members[0]["path"]
    right = root_path / members[1]["path"]
    try:
        argv = diff_command(left, right)
    except EditorTemplateError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e
    if argv is None:
        app_context.console.print(
            style(
                cs.CLI_ERR_DUPLICATES_OPEN_NO_TOOL.format(editor=resolve_editor()),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)
    try:
        subprocess.Popen(  # noqa: S603 - user-configured local editor command
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as e:
        app_context.console.print(
            style(
                cs.CLI_ERR_DUPLICATES_OPEN_NO_TOOL.format(editor=argv[0]), cs.Color.RED
            )
        )
        raise typer.Exit(1) from e
    app_context.console.print(
        style(
            cs.CLI_DUPLICATES_OPENED_DIFF.format(left=left, right=right), cs.Color.GREEN
        )
    )
    if len(members) > cs.DUPLICATES_OPEN_PAIR_SIZE:
        app_context.console.print(
            style(
                cs.CLI_DUPLICATES_OPEN_EXTRA_MEMBERS.format(
                    number=number, count=len(members)
                ),
                cs.Color.YELLOW,
            )
        )


@app.command(
    name=ch.CLICommandName.DUPLICATES,
    help=ch.DESC_DUPLICATES,
    short_help=ch.CMD_DUPLICATES,
    epilog=ch.EXAMPLES_DUPLICATES,
    rich_help_panel=ch.PANEL_GRAPH,
)
def duplicates(
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_SCAN_PROJECT
    ),
    # The spelling before issue #2415, kept working for scripts.
    project_name: str | None = typer.Option(
        None, cs.CLI_OPT_PROJECT_NAME, "-n", hidden=True
    ),
    repo_path: Path | None = typer.Option(
        None, "--repo-path", exists=True, file_okay=False, help=ch.HELP_SCAN_REPO_PATH
    ),
    threshold: float = typer.Option(
        cs.DUPLICATES_DEFAULT_THRESHOLD,
        "--threshold",
        min=0.0,
        max=1.0,
        help=ch.HELP_DUPLICATES_THRESHOLD,
    ),
    min_size: int = typer.Option(
        cs.DUPLICATES_DEFAULT_MIN_NODES,
        "--min-size",
        min=1,
        help=ch.HELP_DUPLICATES_MIN_SIZE,
    ),
    exact_only: bool = typer.Option(
        False, "--exact-only", help=ch.HELP_DUPLICATES_EXACT_ONLY
    ),
    exclude: list[str] = typer.Option([], "--exclude", help=ch.HELP_DUPLICATES_EXCLUDE),
    output_format: cs.DuplicatesFormat = typer.Option(
        cs.DuplicatesFormat.TABLE, "--format", help=ch.HELP_DUPLICATES_FORMAT
    ),
    output: Path | None = typer.Option(
        None, "--output", "-o", help=ch.HELP_DUPLICATES_OUTPUT
    ),
    fail_on_found: bool = typer.Option(
        False, "--fail-on-found", help=ch.HELP_DUPLICATES_FAIL_ON_FOUND
    ),
    open_group: int | None = typer.Option(
        None, "--open", min=1, help=ch.HELP_DUPLICATES_OPEN
    ),
) -> None:
    from .duplicates import collect_duplicates_with_coverage

    chosen = _scan_project_option(project, project_name)
    show_progress = (
        output_format == cs.DuplicatesFormat.TABLE
        and output is None
        and not settings.QUIET
    )
    if show_progress:
        app_context.console.print(style(cs.CLI_DUPLICATES_CONNECTING, cs.Color.CYAN))

    choice = ProjectChoice(None)
    roots: dict[str, str | None] = {}
    report = DuplicatesReport(groups=[], skipped_symbols=0, truncated=False)
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = ingestor.list_projects()
            # Roots ride along for clickable locations and --open; a graph
            # predating Project.root_path just degrades to plain text.
            roots = project_roots_from_rows(ingestor.fetch_all(cq.CYPHER_LIST_PROJECTS))
            choice = choose_project(chosen, repo_path, projects, ingestor)
            if (resolved := choice.name) is not None:
                logger.info(ls.DUPLICATES_SCANNING.format(project_name=resolved))
                report = collect_duplicates_with_coverage(
                    ingestor,
                    resolved,
                    DuplicatesConfig(
                        threshold=threshold,
                        min_nodes=min_size,
                        exact_only=exact_only,
                        exclude_patterns=tuple(exclude),
                    ),
                )
    except GraphUnavailableError:
        raise
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_DUPLICATES_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.DUPLICATES_ERROR.format(error=e))
        raise typer.Exit(1) from e

    resolved = _require_project(choice)
    root = roots.get(resolved)
    root_path = Path(root) if root else None
    _emit_duplicates(
        report.groups,
        output_format,
        output,
        resolved,
        report.skipped_symbols,
        report.truncated,
        report.analyzed_symbols,
        root_path,
    )

    if open_group is not None:
        _open_duplicate_group(report.groups, open_group, root_path, resolved)

    if fail_on_found and report.groups:
        raise typer.Exit(1)


def _project_to_delete(explicit: str | None, repo: Path | None) -> str:
    """The project `delete-project` was asked for, named before connecting.

    Unlike the commands that only read, a delete is never implied by the
    directory the shell stands in (issue #2415): it takes a name or an
    explicit --repo-path, and a blank name is no name.
    """
    if explicit is not None:
        if not explicit.strip():
            _exit_with_error(cs.CLI_ERR_PROJECT_NAME_REQUIRED)
        return explicit.strip()
    if repo is None:
        _exit_with_error(cs.CLI_ERR_PROJECT_NAME_REQUIRED)
    return derive_project_name(repo)


@app.command(
    name=ch.CLICommandName.DELETE_PROJECT,
    help=ch.CMD_DELETE_PROJECT,
    short_help=ch.CMD_DELETE_PROJECT,
    epilog=ch.EXAMPLES_DELETE_PROJECT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def delete_project(
    project: str | None = typer.Option(
        None, "--project", "-p", help=ch.HELP_DELETE_PROJECT
    ),
    # The spelling before issue #2415, kept working for scripts.
    name: str | None = typer.Option(None, cs.CLI_OPT_NAME, "-n", hidden=True),
    repo_path: str | None = typer.Option(
        None,
        "--repo-path",
        help=ch.HELP_DELETE_PROJECT_REPO_PATH,
    ),
) -> None:
    explicit = _one_project(project, name, cs.CLI_OPT_NAME)
    repo = Path(repo_path) if repo_path else None
    project_name = _project_to_delete(explicit, repo)
    effective_batch_size = settings.resolve_batch_size(None)

    try:
        with connect_memgraph(effective_batch_size) as ingestor:
            projects = ingestor.list_projects()
            choice = choose_project(
                None if explicit is None else project_name,
                repo,
                projects,
                ingestor,
            )
            if choice.name is None:
                app_context.console.print(style(choice.refusal, cs.Color.RED))
                raise typer.Exit(1)

            # A directory indexed under two names used to lose both on delete
            # (#2665). The delete is ownership-scoped now; say so before it runs.
            roots = ingestor.list_project_roots()
            if sharing := projects_sharing_root(project_name, roots):
                app_context.console.print(
                    style(
                        cs.CLI_WARN_DELETE_PROJECT_SHARED_ROOT.format(
                            projects=", ".join(sharing),
                            project_name=project_name,
                            root_path=roots[project_name],
                        ),
                        cs.Color.YELLOW,
                    )
                )

            _info(
                style(
                    cs.CLI_MSG_DELETING_PROJECT.format(project_name=project_name),
                    cs.Color.YELLOW,
                )
            )
            _cleanup_project_embeddings(ingestor, project_name, projects)
            try:
                ingestor.delete_project(project_name)
            except Exception:
                _sweep_glosses_after_failed_delete(ingestor, project_name)
                raise
            # A Gloss is out of the delete's reach by design, which is what
            # lets it survive an index's delete-then-rebuild, so the
            # deliberate delete sweeps the project's notes, as MCP
            # `delete_project` does. Without it the next index re-attached
            # them by name (issue #3231). The sweep never raises: the delete
            # is done, and a retry would stop at "project not found".
            if not prune_orphaned_glosses(ingestor, project_name):
                app_context.console.print(
                    style(
                        cs.CLI_WARN_GLOSSES_NOT_PRUNED.format(
                            project_name=project_name
                        ),
                        cs.Color.YELLOW,
                    )
                )
            _clear_deleted_project_markers(ingestor, project_name)
    except typer.Exit:
        raise
    except GraphUnavailableError:
        raise
    except Exception as e:
        app_context.console.print(
            style(
                cs.CLI_ERR_DELETE_PROJECT_FAILED.format(
                    project_name=project_name, error=e
                ),
                cs.Color.RED,
            )
        )
        logger.exception(
            cs.CLI_ERR_DELETE_PROJECT_FAILED.format(project_name=project_name, error=e)
        )
        raise typer.Exit(1) from e

    if repo_path:
        _delete_hash_cache(Path(repo_path))

    _info(
        style(
            cs.CLI_MSG_PROJECT_DELETED.format(project_name=project_name),
            cs.Color.GREEN,
        )
    )


if __name__ == "__main__":
    app()
