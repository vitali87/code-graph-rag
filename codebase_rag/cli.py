import asyncio
import json
import time
from collections.abc import Callable
from fnmatch import fnmatch
from functools import partial
from importlib.metadata import version as get_version
from pathlib import Path

import typer
from loguru import logger
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import cgr_state
from . import cli_help as ch
from . import constants as cs
from . import logs as ls
from .config import load_ignore_patterns, settings
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
from .services.graph_service import MemgraphIngestor
from .services.protobuf_service import ProtobufFileIngestor
from .stack import StackManager
from .stack.cli import cli as daemon_cli
from .stack.constants import StackState
from .stack.manager import StackError
from .tools.health_checker import HealthChecker
from .tools.language import cli as language_cli
from .types_defs import DeadCodeConfig, DeadCodeRow, ResultRow
from .utils.path_utils import derive_project_name, resolve_repo_path
from .vector_store import delete_project_embeddings
from .workspaces import WorkspaceConfig, WorkspaceError, load_workspace
from .workspaces.cli import cli as workspace_cli

app = typer.Typer(
    name=cs.PACKAGE_NAME,
    help=ch.APP_DESCRIPTION,
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
        help="Suppress non-essential output (progress messages, banners, informational logs).",
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
        return
    try:
        mgr.ensure_running()
    except StackError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e


def _run_graph_sync(
    repo: Path,
    project_name: str,
    batch_size: int,
    exclude: list[str] | None,
    interactive_setup: bool,
    clean: bool = False,
    output: str | None = None,
    skip_embeddings: bool | None = None,
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
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()
            _delete_hash_cache(repo)

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
    dir_mtimes_path = repo_path / cs.DIR_MTIMES_FILENAME
    dir_mtimes_path.unlink(missing_ok=True)


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


@app.command(help=ch.CMD_START)
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
            _info(style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW))
            ingestor.clean_database()

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
            skip_embeddings=no_embeddings or None,
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


@app.command(help=ch.CMD_INDEX)
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
        ingestor = ProtobufFileIngestor(
            output_path=output_proto_dir, split_index=split_index
        )
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor=ingestor,
            repo_path=repo_to_index,
            parsers=parsers,
            queries=queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
        )

        updater.run()
        _info(style(cs.CLI_MSG_INDEXING_DONE, cs.Color.GREEN))

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_INDEXING.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.INDEXING_FAILED)
        raise typer.Exit(1) from e


@app.command(help=ch.CMD_EXPORT)
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


@app.command(help=ch.CMD_OPTIMIZE)
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


@app.command(name=ch.CLICommandName.MCP_SERVER, help=ch.CMD_MCP_SERVER)
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


@app.command(name=ch.CLICommandName.GRAPH_LOADER, help=ch.CMD_GRAPH_LOADER)
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


@app.command(
    name=ch.CLICommandName.LANGUAGE,
    help=ch.CMD_LANGUAGE,
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def language_command(ctx: typer.Context) -> None:
    language_cli(ctx.args, standalone_mode=False)


@app.command(
    name=ch.CLICommandName.DAEMON,
    help=ch.CMD_DAEMON,
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def daemon_command(ctx: typer.Context) -> None:
    daemon_cli(ctx.args, standalone_mode=False)


@app.command(
    name=ch.CLICommandName.WORKSPACE,
    help=ch.CMD_WORKSPACE,
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def workspace_command(ctx: typer.Context) -> None:
    workspace_cli(ctx.args, standalone_mode=False)


@app.command(name=ch.CLICommandName.STOP, help=ch.CMD_STOP)
def stop_command() -> None:
    mgr = StackManager()
    try:
        mgr.down()
    except StackError as e:
        app_context.console.print(style(str(e), cs.Color.RED))
        raise typer.Exit(1) from e
    _info(style("stack stopped", cs.Color.GREEN))


@app.command(name=ch.CLICommandName.STATUS, help=ch.CMD_STATUS)
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


@app.command(name=ch.CLICommandName.DOCTOR, help=ch.CMD_DOCTOR)
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


@app.command(name=ch.CLICommandName.STATS, help=ch.CMD_STATS)
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
) -> DeadCodeConfig:
    # (H) test_patterns is always set: with tests included it makes test
    # (H) functions roots; with tests excluded it filters test modules out of the
    # (H) module-load roots so test-only code is not kept alive.
    return DeadCodeConfig(
        include_tests=include_tests,
        include_classes=include_classes,
        root_decorators=frozenset(
            {d.lower() for d in cs.DEFAULT_ROOT_DECORATORS}
            | {d.lower() for d in decorator_roots}
        ),
        entry_points=tuple(entry_points),
        test_patterns=tuple(cs.TEST_PATH_PATTERNS),
    )


def _filter_excluded_rows(rows: list[ResultRow], exclude: list[str]) -> list[ResultRow]:
    # (H) Drop candidates whose file path matches an exclude glob (generated dirs
    # (H) like client/core or *.gen.* have no in-repo caller, so every symbol
    # (H) reports as dead). fnmatch treats '*' as spanning '/', so '*client/core*'
    # (H) matches at any depth.
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

    table = _build_dead_code_table(candidates, project_name)
    if output is not None:
        with output.open("w", encoding=cs.ENCODING_UTF8) as fh:
            Console(file=fh).print(table)
        app_context.console.print(
            style(
                cs.CLI_DEADCODE_WRITTEN.format(count=len(candidates), path=output),
                cs.Color.GREEN,
            )
        )
        return

    if not candidates:
        app_context.console.print(style(cs.CLI_DEADCODE_NONE, cs.Color.GREEN))
        return
    app_context.console.print(table)
    app_context.console.print(
        style(cs.CLI_DEADCODE_SUMMARY.format(count=len(candidates)), cs.Color.GREEN)
    )


@app.command(name=ch.CLICommandName.DEAD_CODE, help=ch.CMD_DEAD_CODE)
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
) -> None:
    from .dead_code import collect_dead_code

    show_progress = output_format == cs.DeadCodeFormat.TABLE and output is None
    if show_progress:
        app_context.console.print(style(cs.CLI_DEADCODE_CONNECTING, cs.Color.CYAN))

    projects: list[str] = []
    resolved: str | None = None
    rows: list[ResultRow] = []
    try:
        with connect_memgraph(batch_size=1) as ingestor:
            projects = ingestor.list_projects()
            resolved = _resolve_dead_code_project(project_name, projects)
            if resolved is not None:
                logger.info(ls.DEADCODE_SCANNING.format(project_name=resolved))
                rows = collect_dead_code(
                    ingestor,
                    resolved,
                    _dead_code_config(
                        include_tests, include_classes, entry_point, decorator_root
                    ),
                )
    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_DEADCODE_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.DEADCODE_ERROR.format(error=e))
        raise typer.Exit(1) from e

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
    _emit_dead_code(candidates, output_format, output, resolved)

    if fail_on_found and candidates:
        raise typer.Exit(1)


@app.command(name=ch.CLICommandName.DELETE_PROJECT, help=ch.CMD_DELETE_PROJECT)
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
