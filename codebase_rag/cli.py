"""Command-line entry point wiring cgr subcommands to their handlers."""

import asyncio
import importlib
import json
import subprocess
import sys
import time
from collections.abc import Callable
from fnmatch import fnmatch
from functools import partial
from importlib.metadata import version as get_version
from pathlib import Path

import click
import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import cgr_state
from . import cli_help as ch
from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .capture import CaptureSelection, resolve_capture, split_spec
from .config import load_ignore_patterns, settings
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
from .graph_updater import GraphUpdater
from .main import (
    _create_configuration_table,
    app_context,
    connect_memgraph,
    export_graph_to_file,
    main_async,
    main_optimize_async,
    main_single_query,
    prompt_for_unignored_directories,
    style,
    update_model_settings,
)
from .parser_loader import load_parsers
from .services.graph_diff import DiffError, diff_indexes, diff_is_empty
from .services.graph_service import MemgraphIngestor
from .services.protobuf_service import ProtobufFileIngestor
from .services.provenance import (
    capture_description,
    source_state,
    verify_index,
    write_manifest,
)
from .stack import StackManager
from .stack.cli import cli as daemon_cli
from .stack.constants import StackState
from .stack.manager import StackError
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
    ResultRow,
)
from .utils.path_utils import (
    derive_project_name,
    project_roots_from_rows,
    resolve_repo_path,
)
from .vector_store import clear_all_embeddings, delete_project_embeddings
from .workspaces import WorkspaceConfig, WorkspaceError, load_workspace
from .workspaces.cli import cli as workspace_cli


def _vendored_click_exception() -> type[click.ClickException]:
    # A typer that vendors click raises the vendored exceptions, which do not
    # descend from the real click's; both flavors must be caught (#1409).
    try:
        vendored = importlib.import_module(cs.TYPER_VENDORED_CLICK_EXCEPTIONS_MODULE)
    except ImportError:
        return click.ClickException
    return getattr(vendored, "ClickException", click.ClickException)


_CLICK_EXCEPTIONS = (click.ClickException, _vendored_click_exception())

app = typer.Typer(
    name=cs.PACKAGE_NAME,
    help=ch.APP_DESCRIPTION,
    epilog=ch.APP_EPILOG,
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        app_context.console.print(
            cs.CLI_MSG_VERSION.format(
                package=cs.PACKAGE_NAME, version=get_version(cs.PACKAGE_NAME)
            ),
            highlight=False,
        )
        raise typer.Exit()


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


@app.callback()
def _global_options(
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
    settings.QUIET = quiet
    if quiet:
        logger.remove()
        logger.add(lambda msg: app_context.console.print(msg, end=""), level="ERROR")


def _info(msg: str) -> None:
    if not settings.QUIET:
        app_context.console.print(msg)


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
            batch_size=batch_size,
            exclude=exclude,
            interactive_setup=False,
            capture=capture,
            skip_embeddings=skip_embeddings,
        )


def _resolve_active_projects(projects: str | None, default_project: str) -> list[str]:
    if projects:
        parsed = [p.strip() for p in projects.split(",") if p.strip()]
        if parsed:
            return parsed
    return [default_project]


def _maybe_start_stack() -> None:
    mgr = StackManager()
    if mgr.status().state == StackState.RUNNING:
        # This early return bypasses ensure_running, so it needs its own
        # public-port check for a stack that is already up (issue #1380).
        mgr.warn_if_ports_are_public()
        return
    try:
        mgr.ensure_running()
    except StackError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e


def _capture_selection(capture: list[str] | None) -> CaptureSelection:
    # Env CGR_CAPTURE is the sticky baseline; --capture tokens are appended so
    # a single run can override it (later tokens win in the resolver).
    return resolve_capture([*split_spec(settings.CGR_CAPTURE), *(capture or [])])


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


def _run_graph_sync(
    repo: Path,
    project_name: str,
    batch_size: int,
    exclude: list[str] | None,
    interactive_setup: bool,
    clean: bool = False,
    output: str | None = None,
    capture: list[str] | None = None,
    skip_embeddings: bool | None = None,
    assume_yes: bool = False,
) -> None:
    cgrignore = load_ignore_patterns(repo)
    cli_excludes = frozenset(exclude) if exclude else frozenset()
    exclude_paths = cli_excludes | cgrignore.exclude or None
    unignore_paths: frozenset[str] | None
    if interactive_setup:
        unignore_paths = prompt_for_unignored_directories(repo, exclude)
    else:
        unignore_paths = cgrignore.unignore or None

    elapsed = time.monotonic()
    with connect_memgraph(batch_size) as ingestor:
        if clean:
            _confirm_destructive_clean(ingestor, project_name, assume_yes)
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()
            _delete_hash_cache(repo)
            # Stale vectors keyed by recycled node ids would crowd out live
            # hits and can map onto unrelated nodes in the rebuilt graph.
            clear_all_embeddings()

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
            capture=_capture_selection(capture),
            skip_embeddings=skip_embeddings,
        )
        updater.run()
        cgr_state.record_sync(project_name)

        if output:
            _info(style(cs.CLI_MSG_EXPORTING_TO.format(path=output), cs.Color.CYAN))
            if not export_graph_to_file(ingestor, output):
                raise typer.Exit(1)
    elapsed = time.monotonic() - elapsed
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


def _delete_hash_cache(repo_path: Path) -> None:
    cache_path = repo_path / cs.HASH_CACHE_FILENAME
    if cache_path.exists():
        _info(
            style(
                cs.CLI_MSG_CLEANING_HASH_CACHE.format(path=cache_path),
                cs.Color.YELLOW,
            )
        )
        cache_path.unlink(missing_ok=True)
    (repo_path / cs.DIR_MTIMES_FILENAME).unlink(missing_ok=True)
    (repo_path / cs.PARSER_FINGERPRINT_FILENAME).unlink(missing_ok=True)
    (repo_path / cs.EXCLUSION_STATE_FILENAME).unlink(missing_ok=True)


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


def _cleanup_project_embeddings(ingestor: MemgraphIngestor, project_name: str) -> None:
    rows = ingestor.fetch_all(
        cs.CYPHER_QUERY_PROJECT_NODE_IDS,
        {cs.KEY_PROJECT_NAME: project_name},
    )
    node_ids: list[int] = []
    for row in rows:
        node_id = row.get(cs.KEY_NODE_ID)
        if isinstance(node_id, int):
            node_ids.append(node_id)
    delete_project_embeddings(project_name, node_ids)


@app.command(
    help=ch.CMD_START,
    short_help=ch.CMD_START,
    epilog=ch.EXAMPLES_START,
    rich_help_panel=ch.PANEL_USE,
)
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
    ),
    exclude: list[str] | None = typer.Option(
        None,
        "--exclude",
        help=ch.HELP_EXCLUDE_PATTERNS,
    ),
    capture: list[str] | None = typer.Option(
        None,
        "--capture",
        help=ch.HELP_CAPTURE,
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

    if output_format == cs.QueryFormat.JSON and not ask_agent:
        app_context.console.print(
            style(cs.CLI_ERR_JSON_REQUIRES_ASK_AGENT, cs.Color.RED)
        )
        raise typer.Exit(1)

    resolved_repo = _resolve_and_validate_repo(repo_path)
    target_repo_path = str(resolved_repo)
    resolved_project_name = project_name or derive_project_name(resolved_repo)

    if output and not update_graph:
        app_context.console.print(
            style(cs.CLI_ERR_OUTPUT_REQUIRES_UPDATE, cs.Color.RED)
        )
        raise typer.Exit(1)

    if not no_start_stack:
        _maybe_start_stack()

    effective_batch_size = settings.resolve_batch_size(batch_size)

    if clean and not update_graph:
        repo_to_clean = Path(target_repo_path)
        with connect_memgraph(effective_batch_size) as ingestor:
            _confirm_destructive_clean(ingestor, resolved_project_name, yes)
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()

        clear_all_embeddings()
        _delete_hash_cache(repo_to_clean)
        _info(style(cs.CLI_MSG_CLEAN_DONE, cs.Color.GREEN))
        return

    _update_and_validate_models(orchestrator, cypher)

    if not ask_agent and not update_graph:
        app_context.console.print(_create_configuration_table(target_repo_path))

    if update_graph:
        _info(
            style(cs.CLI_MSG_UPDATING_GRAPH.format(path=resolved_repo), cs.Color.GREEN)
        )
        if not interactive_setup:
            _info(style(cs.CLI_MSG_AUTO_EXCLUDE, cs.Color.YELLOW))
        _run_graph_sync(
            repo=resolved_repo,
            project_name=resolved_project_name,
            batch_size=effective_batch_size,
            exclude=exclude,
            interactive_setup=interactive_setup,
            clean=clean,
            output=output,
            capture=capture,
            skip_embeddings=no_embeddings or None,
            assume_yes=yes,
        )
        _info(style(cs.CLI_MSG_GRAPH_UPDATED, cs.Color.GREEN))
        return

    workspace_config = _load_workspace_or_exit(workspace)

    sync_task: Callable[[], None] | None = None
    sync_message = cs.MSG_SYNCING_KNOWLEDGE_GRAPH
    if not no_sync:
        if workspace_config is not None:
            sync_task = partial(
                _sync_workspace,
                workspace_config,
                effective_batch_size,
                exclude,
                capture=capture,
                skip_embeddings=no_embeddings or None,
            )
            sync_message = cs.MSG_SYNCING_WORKSPACE.format(
                name=workspace_config.name, count=len(workspace_config.repos)
            )
        else:
            sync_task = partial(
                _run_graph_sync,
                repo=resolved_repo,
                project_name=resolved_project_name,
                batch_size=effective_batch_size,
                exclude=exclude,
                interactive_setup=interactive_setup,
                capture=capture,
                skip_embeddings=no_embeddings or None,
            )

    if workspace_config is not None:
        active_projects = workspace_config.project_names()
        if projects:
            active_projects = _resolve_active_projects(projects, active_projects[0])
    else:
        active_projects = _resolve_active_projects(projects, resolved_project_name)

    try:
        if ask_agent:
            if sync_task is not None:
                sync_task()
            main_single_query(
                target_repo_path,
                effective_batch_size,
                ask_agent,
                active_projects=active_projects,
                output_format=output_format,
            )
        else:
            asyncio.run(
                main_async(
                    target_repo_path,
                    effective_batch_size,
                    active_projects=active_projects,
                    show_config_table=False,
                    pre_chat_sync=sync_task,
                    pre_chat_sync_message=sync_message,
                )
            )
    except KeyboardInterrupt:
        app_context.console.print(style(cs.CLI_MSG_APP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_STARTUP.format(error=e), cs.Color.RED)
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
        help=ch.HELP_CAPTURE,
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
        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_to_index,
            parsers=parsers,
            queries=queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            capture=capture_config,
        )

        updater.run()
        manifest_path = write_manifest(
            Path(output_proto_dir),
            indexed_source,
            capture_description(capture_config),
        )
        _info(
            style(cs.CLI_MSG_MANIFEST_WRITTEN.format(path=manifest_path), cs.Color.CYAN)
        )
        _info(style(cs.CLI_MSG_INDEXING_DONE, cs.Color.GREEN))

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_INDEXING.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.INDEXING_FAILED)
        raise typer.Exit(1) from e


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
    format_json: bool = typer.Option(
        True, "--json/--no-json", help=ch.HELP_FORMAT_JSON
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
    ),
) -> None:
    if not format_json:
        app_context.console.print(style(cs.CLI_ERR_ONLY_JSON, cs.Color.RED))
        raise typer.Exit(1)

    _info(style(cs.CLI_MSG_CONNECTING_MEMGRAPH, cs.Color.CYAN))

    effective_batch_size = settings.resolve_batch_size(batch_size)

    try:
        with connect_memgraph(effective_batch_size) as ingestor:
            _info(style(cs.CLI_MSG_EXPORTING_DATA, cs.Color.CYAN))

            if not export_graph_to_file(ingestor, output):
                raise typer.Exit(1)

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_EXPORT_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.EXPORT_ERROR.format(error=e))
        raise typer.Exit(1) from e


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
) -> None:
    try:
        if transport == cs.MCPTransport.HTTP:
            from codebase_rag.mcp import serve_http

            resolved_host = host or settings.MCP_HTTP_HOST
            resolved_port = port or settings.MCP_HTTP_PORT
            asyncio.run(serve_http(host=resolved_host, port=resolved_port))
        else:
            from codebase_rag.mcp import serve_stdio

            asyncio.run(serve_stdio())
    except KeyboardInterrupt:
        app_context.console.print(style(cs.CLI_MSG_APP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_CONFIG.format(error=e), cs.Color.RED)
        )
        _info(style(cs.CLI_MSG_HINT_TARGET_REPO, cs.Color.YELLOW))
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_MCP_SERVER.format(error=e), cs.Color.RED)
        )


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
    group.main(
        args=list(ctx.args),
        prog_name=ctx.command_path,
        standalone_mode=False,
    )


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
    project: str | None = typer.Option(None, "--project", help=ch.HELP_GRAPH_PROJECT),
    fail_on_found: bool = typer.Option(
        False, "--fail-on-found", help=ch.HELP_CHECK_FAIL_ON_FOUND
    ),
) -> None:
    from .structural_check import CheckError, run_check
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
            delta = run_check(root, base, name, ingestor, parsers, queries)
        except CheckError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=1) from error
    typer.echo(json.dumps(delta, indent=cs.MCP_JSON_INDENT))
    if fail_on_found and has_findings(delta):
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
    timestamps = cgr_state.read_sync_timestamps()
    if not timestamps:
        app_context.console.print("syncs:    (no projects synced via cgr yet)")
        return
    app_context.console.print("syncs:")
    for project, ts in sorted(timestamps.items()):
        app_context.console.print(f"  - {project}: last sync {ts}")


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

    for result in results:
        status = "✓" if result.passed else "✗"
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
    name=ch.CLICommandName.STATS,
    help=ch.CMD_STATS,
    short_help=ch.CMD_STATS,
    rich_help_panel=ch.PANEL_GRAPH,
)
def stats() -> None:
    from .cypher_queries import (
        CYPHER_STATS_NODE_COUNTS,
        CYPHER_STATS_RELATIONSHIP_COUNTS,
    )

    app_context.console.print(style(cs.CLI_MSG_CONNECTING_STATS, cs.Color.CYAN))

    try:
        with connect_memgraph(batch_size=1) as ingestor:
            node_results = ingestor.fetch_all(CYPHER_STATS_NODE_COUNTS)
            rel_results = ingestor.fetch_all(CYPHER_STATS_RELATIONSHIP_COUNTS)

            app_context.console.print(
                _build_stats_table(
                    cs.CLI_STATS_NODE_TITLE,
                    cs.CLI_STATS_COL_NODE_TYPE,
                    node_results,
                    lambda r: ":".join(r.get("labels", [])) or cs.CLI_STATS_UNKNOWN,
                    cs.CLI_STATS_TOTAL_NODES,
                )
            )
            app_context.console.print()
            app_context.console.print(
                _build_stats_table(
                    cs.CLI_STATS_REL_TITLE,
                    cs.CLI_STATS_COL_REL_TYPE,
                    rel_results,
                    lambda r: str(r.get("type", cs.CLI_STATS_UNKNOWN)),
                    cs.CLI_STATS_TOTAL_RELS,
                )
            )

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_STATS_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.STATS_ERROR.format(error=e))
        raise typer.Exit(1) from e


def _resolve_dead_code_project(
    project_name: str | None, projects: list[str]
) -> str | None:
    if project_name:
        return project_name.strip()
    if len(projects) == 1:
        return projects[0]
    return None


def _dead_code_config(
    include_tests: bool,
    include_classes: bool,
    entry_points: list[str],
    decorator_roots: list[str],
    min_resolution: cs.EdgeResolution | None = None,
) -> DeadCodeConfig:
    # test_patterns is always set: included tests become roots; excluded, it
    # filters test modules out of module-load roots so test-only code stays dead.
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(
            {d.lower() for d in cs.DEFAULT_ROOT_DECORATORS}
            | {d.lower() for d in decorator_roots}
        ),
        entry_points=tuple(entry_points),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
        min_resolution=str(min_resolution) if min_resolution is not None else None,
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
        start_line=int(start) if isinstance(start, int | float) else 0,
        end_line=int(end) if isinstance(end, int | float) else 0,
    )


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
    table.add_column(cs.CLI_DEADCODE_COL_QUALIFIED_NAME, style=cs.Color.CYAN)
    table.add_column(cs.CLI_DEADCODE_COL_LINES, style=cs.Color.YELLOW, justify="right")
    for row in candidates:
        table.add_row(
            row["label"],
            row["qualified_name"],
            cs.CLI_DEADCODE_LINE_RANGE.format(
                start=row["start_line"], end=row["end_line"]
            ),
        )
    return table


def _emit_dead_code(
    candidates: list[DeadCodeRow],
    output_format: cs.DeadCodeFormat,
    output: Path | None,
    project_name: str,
    structural_tier_symbols: int = 0,
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
        with output.open("w", encoding=cs.ENCODING_UTF8) as fh:
            file_console = Console(file=fh)
            file_console.print(table)
            if notice:
                file_console.print(notice)
        app_context.console.print(
            style(
                cs.CLI_DEADCODE_WRITTEN.format(count=len(candidates), path=output),
                cs.Color.GREEN,
            )
        )
        if notice:
            app_context.console.print(style(notice, cs.Color.YELLOW))
        return

    if not candidates:
        app_context.console.print(style(cs.CLI_DEADCODE_NONE, cs.Color.GREEN))
    else:
        app_context.console.print(table)
        app_context.console.print(
            style(cs.CLI_DEADCODE_SUMMARY.format(count=len(candidates)), cs.Color.GREEN)
        )
    if notice:
        app_context.console.print(style(notice, cs.Color.YELLOW))


@app.command(
    name=ch.CLICommandName.DEAD_CODE,
    help=ch.CMD_DEAD_CODE,
    short_help=ch.CMD_DEAD_CODE,
    epilog=ch.EXAMPLES_DEAD_CODE,
    rich_help_panel=ch.PANEL_GRAPH,
)
def dead_code(
    project_name: str | None = typer.Option(
        None, "--project-name", "-n", help=ch.HELP_DEADCODE_PROJECT_NAME
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
) -> None:
    from .dead_code import collect_dead_code_with_coverage

    show_progress = output_format == cs.DeadCodeFormat.TABLE and output is None
    if show_progress:
        app_context.console.print(style(cs.CLI_DEADCODE_CONNECTING, cs.Color.CYAN))

    projects: list[str] = []
    resolved: str | None = None
    rows: list[ResultRow] = []
    structural_tier_symbols = 0
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = ingestor.list_projects()
            resolved = _resolve_dead_code_project(project_name, projects)
            if resolved is not None and resolved in projects:
                logger.info(ls.DEADCODE_SCANNING.format(project_name=resolved))
                rows, structural_tier_symbols = collect_dead_code_with_coverage(
                    ingestor,
                    resolved,
                    _dead_code_config(
                        include_tests,
                        include_classes,
                        entry_point,
                        decorator_root,
                        min_resolution,
                    ),
                )
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_DEADCODE_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.DEADCODE_ERROR.format(error=e))
        raise typer.Exit(1) from e

    # An explicit name absent from the graph must error, not scan a
    # nonexistent prefix and report a clean project (the duplicates command
    # gained this guard first). Raised OUTSIDE the connection context so a
    # user typo never trips the service layer's error logging on exit.
    if resolved is not None and resolved not in projects:
        app_context.console.print(
            style(
                cs.CLI_ERR_DEADCODE_UNKNOWN_PROJECT.format(
                    project=resolved, projects=projects
                ),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)

    if resolved is None:
        message = (
            cs.CLI_ERR_DEADCODE_NO_PROJECTS
            if not projects
            else cs.CLI_ERR_DEADCODE_AMBIGUOUS_PROJECT.format(projects=projects)
        )
        app_context.console.print(style(message, cs.Color.RED))
        raise typer.Exit(1)

    candidates = [
        _to_dead_code_row(row) for row in _filter_excluded_rows(rows, exclude)
    ]
    _emit_dead_code(
        candidates, output_format, output, resolved, structural_tier_symbols
    )

    if fail_on_found and candidates:
        raise typer.Exit(1)


def _similarity_text(group: DuplicateGroup) -> str:
    if group["kind"] == cs.KIND_EXACT:
        return cs.CLI_DUPLICATES_SIMILARITY_EXACT
    return cs.CLI_DUPLICATES_SIMILARITY_PCT.format(pct=group["similarity"] * 100)


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
    table.add_column(cs.CLI_DUPLICATES_COL_MEMBER, style=cs.Color.CYAN)
    table.add_column(cs.CLI_DUPLICATES_COL_LOCATION, style=cs.Color.YELLOW)
    for number, group in enumerate(groups, start=1):
        for at, member in enumerate(group["members"]):
            table.add_row(
                _duplicates_group_cell(number, group, root_path) if at == 0 else "",
                group["kind"] if at == 0 else "",
                _similarity_text(group) if at == 0 else "",
                member["qualified_name"],
                _duplicates_location_cell(member, root_path),
            )
        table.add_section()
    return table


def _emit_duplicates_json(
    groups: list[DuplicateGroup],
    output: Path | None,
    skipped_symbols: int,
    truncated: bool,
) -> None:
    # Envelope, not a bare list: scan-completeness metadata must reach
    # JSON consumers too, or a CI artifact reads as a complete scan when
    # symbols went unanalyzed or group enumeration hit its cap.
    payload = json.dumps(
        {
            cs.KEY_DUPLICATE_GROUPS: groups,
            cs.KEY_SKIPPED_SYMBOLS: skipped_symbols,
            cs.KEY_TRUNCATED: truncated,
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
        file_console.print(table)
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
        app_context.console.print(table)
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
    help=ch.CMD_DUPLICATES,
    short_help=ch.CMD_DUPLICATES,
    epilog=ch.EXAMPLES_DUPLICATES,
    rich_help_panel=ch.PANEL_GRAPH,
)
def duplicates(
    project_name: str | None = typer.Option(
        None, "--project-name", "-n", help=ch.HELP_DUPLICATES_PROJECT_NAME
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

    show_progress = output_format == cs.DuplicatesFormat.TABLE and output is None
    if show_progress:
        app_context.console.print(style(cs.CLI_DUPLICATES_CONNECTING, cs.Color.CYAN))

    projects: list[str] = []
    resolved: str | None = None
    roots: dict[str, str | None] = {}
    report = DuplicatesReport(groups=[], skipped_symbols=0, truncated=False)
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = ingestor.list_projects()
            # Roots ride along for clickable locations and --open; a graph
            # predating Project.root_path just degrades to plain text.
            roots = project_roots_from_rows(ingestor.fetch_all(cq.CYPHER_LIST_PROJECTS))
            resolved = _resolve_dead_code_project(project_name, projects)
            if resolved is not None and resolved in projects:
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
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_DUPLICATES_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.DUPLICATES_ERROR.format(error=e))
        raise typer.Exit(1) from e

    # An explicit name absent from the graph must error, not scan a
    # nonexistent prefix and report a clean project. Raised OUTSIDE the
    # connection context: a user typo is not a connection failure and must
    # not trip the service layer's error logging on exit.
    if resolved is not None and resolved not in projects:
        app_context.console.print(
            style(
                cs.CLI_ERR_DUPLICATES_UNKNOWN_PROJECT.format(
                    project=resolved, projects=projects
                ),
                cs.Color.RED,
            )
        )
        raise typer.Exit(1)

    if resolved is None:
        message = (
            cs.CLI_ERR_DEADCODE_NO_PROJECTS
            if not projects
            else cs.CLI_ERR_DEADCODE_AMBIGUOUS_PROJECT.format(projects=projects)
        )
        app_context.console.print(style(message, cs.Color.RED))
        raise typer.Exit(1)

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


@app.command(
    name=ch.CLICommandName.DELETE_PROJECT,
    help=ch.CMD_DELETE_PROJECT,
    short_help=ch.CMD_DELETE_PROJECT,
    epilog=ch.EXAMPLES_DELETE_PROJECT,
    rich_help_panel=ch.PANEL_GRAPH,
)
def delete_project(
    name: str = typer.Option(
        ...,
        "--name",
        "-n",
        help=ch.HELP_DELETE_PROJECT_NAME,
    ),
    repo_path: str | None = typer.Option(
        None,
        "--repo-path",
        help=ch.HELP_DELETE_PROJECT_REPO_PATH,
    ),
) -> None:
    project_name = name.strip()
    if not project_name:
        app_context.console.print(style(cs.CLI_ERR_PROJECT_NAME_REQUIRED, cs.Color.RED))
        raise typer.Exit(1)

    effective_batch_size = settings.resolve_batch_size(None)

    try:
        with connect_memgraph(effective_batch_size) as ingestor:
            projects = ingestor.list_projects()
            if project_name not in projects:
                app_context.console.print(
                    style(
                        cs.CLI_ERR_PROJECT_NOT_FOUND.format(
                            project_name=project_name, projects=projects
                        ),
                        cs.Color.RED,
                    )
                )
                raise typer.Exit(1)

            _info(
                style(
                    cs.CLI_MSG_DELETING_PROJECT.format(project_name=project_name),
                    cs.Color.YELLOW,
                )
            )
            _cleanup_project_embeddings(ingestor, project_name)
            ingestor.delete_project(project_name)
    except typer.Exit:
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
