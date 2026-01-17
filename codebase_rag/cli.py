import asyncio
from pathlib import Path

import typer
from loguru import logger

from . import cli_help as ch
from . import constants as cs
from . import logs as ls
from .config import load_cgrignore_patterns, settings
from .graph_updater import GraphUpdater
from .main import (
    app_context,
    connect_memgraph,
    export_graph_to_file,
    main_async,
    main_optimize_async,
    prompt_for_unignored_directories,
    style,
    update_model_settings,
)
from .cypher_queries import (
    CYPHER_STATS_NODE_COUNTS,
    CYPHER_STATS_RELATIONSHIP_COUNTS,
)
from .parser_loader import load_parsers
from .services.protobuf_service import ProtobufFileIngestor
from .tools.language import cli as language_cli

app = typer.Typer(
    name="graph-code",
    help=ch.APP_DESCRIPTION,
    no_args_is_help=True,
    add_completion=False,
)


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
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
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
    app_context.session.confirm_edits = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    if output and not update_graph:
        app_context.console.print(
            style(cs.CLI_ERR_OUTPUT_REQUIRES_UPDATE, cs.Color.RED)
        )
        raise typer.Exit(1)

    update_model_settings(orchestrator, cypher)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    if update_graph:
        repo_to_update = Path(target_repo_path)
        app_context.console.print(
            style(cs.CLI_MSG_UPDATING_GRAPH.format(path=repo_to_update), cs.Color.GREEN)
        )

        cgrignore = load_cgrignore_patterns(repo_to_update)
        cli_excludes = frozenset(exclude) if exclude else frozenset()
        exclude_paths = cli_excludes | cgrignore.exclude or None
        unignore_paths: frozenset[str] | None = None
        if interactive_setup:
            unignore_paths = prompt_for_unignored_directories(repo_to_update, exclude)
        else:
            app_context.console.print(style(cs.CLI_MSG_AUTO_EXCLUDE, cs.Color.YELLOW))
            unignore_paths = cgrignore.unignore or None

        with connect_memgraph(effective_batch_size) as ingestor:
            if clean:
                app_context.console.print(
                    style(cs.CLI_MSG_CLEANING_DB, cs.Color.YELLOW)
                )
                ingestor.clean_database()
            ingestor.ensure_constraints()

            parsers, queries = load_parsers()

            updater = GraphUpdater(
                ingestor,
                repo_to_update,
                parsers,
                queries,
                unignore_paths,
                exclude_paths,
            )
            updater.run()

            if output:
                app_context.console.print(
                    style(cs.CLI_MSG_EXPORTING_TO.format(path=output), cs.Color.CYAN)
                )
                if not export_graph_to_file(ingestor, output):
                    raise typer.Exit(1)

        app_context.console.print(style(cs.CLI_MSG_GRAPH_UPDATED, cs.Color.GREEN))
        return

    try:
        asyncio.run(main_async(target_repo_path, effective_batch_size))
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
    target_repo_path = repo_path or settings.TARGET_REPO_PATH
    repo_to_index = Path(target_repo_path)

    app_context.console.print(
        style(cs.CLI_MSG_INDEXING_AT.format(path=repo_to_index), cs.Color.GREEN)
    )
    app_context.console.print(
        style(cs.CLI_MSG_OUTPUT_TO.format(path=output_proto_dir), cs.Color.CYAN)
    )

    cgrignore = load_cgrignore_patterns(repo_to_index)
    cli_excludes = frozenset(exclude) if exclude else frozenset()
    exclude_paths = cli_excludes | cgrignore.exclude or None
    unignore_paths: frozenset[str] | None = None
    if interactive_setup:
        unignore_paths = prompt_for_unignored_directories(repo_to_index, exclude)
    else:
        app_context.console.print(style(cs.CLI_MSG_AUTO_EXCLUDE, cs.Color.YELLOW))
        unignore_paths = cgrignore.unignore or None

    try:
        ingestor = ProtobufFileIngestor(
            output_path=output_proto_dir, split_index=split_index
        )
        parsers, queries = load_parsers()
        updater = GraphUpdater(
            ingestor, repo_to_index, parsers, queries, unignore_paths, exclude_paths
        )

        updater.run()

        app_context.console.print(style(cs.CLI_MSG_INDEXING_DONE, cs.Color.GREEN))
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

    app_context.console.print(style(cs.CLI_MSG_CONNECTING_MEMGRAPH, cs.Color.CYAN))

    effective_batch_size = settings.resolve_batch_size(batch_size)

    try:
        with connect_memgraph(effective_batch_size) as ingestor:
            app_context.console.print(style(cs.CLI_MSG_EXPORTING_DATA, cs.Color.CYAN))
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
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help=ch.HELP_BATCH_SIZE,
    ),
) -> None:
    app_context.session.confirm_edits = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

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
        app_context.console.print(
            style(cs.CLI_MSG_OPTIMIZATION_TERMINATED, cs.Color.RED)
        )
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_STARTUP.format(error=e), cs.Color.RED)
        )


@app.command(name=ch.CLICommandName.MCP_SERVER, help=ch.CMD_MCP_SERVER)
def mcp_server() -> None:
    try:
        from codebase_rag.mcp import main as mcp_main

        asyncio.run(mcp_main())
    except KeyboardInterrupt:
        app_context.console.print(style(cs.CLI_MSG_MCP_TERMINATED, cs.Color.RED))
    except ValueError as e:
        app_context.console.print(
            style(cs.CLI_ERR_CONFIG.format(error=e), cs.Color.RED)
        )
        app_context.console.print(style(cs.CLI_MSG_HINT_TARGET_REPO, cs.Color.YELLOW))
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



@app.command(name=ch.CLICommandName.STATS, help=ch.CMD_STATS)
def stats() -> None:
    from rich.table import Table

    app_context.console.print(style(cs.CLI_MSG_CONNECTING_MEMGRAPH, cs.Color.CYAN))

    try:
        with connect_memgraph(batch_size=1) as ingestor:
            node_results = ingestor.fetch_all(CYPHER_STATS_NODE_COUNTS)
            rel_results = ingestor.fetch_all(CYPHER_STATS_RELATIONSHIP_COUNTS)

            total_nodes = sum(int(row.get("count", 0)) for row in node_results)
            total_rels = sum(int(row.get("count", 0)) for row in rel_results)

            node_table = Table(
                title=style(cs.CLI_STATS_NODE_TITLE, cs.Color.GREEN),
                show_header=True,
                header_style=f"bold {cs.Color.MAGENTA}",
            )
            node_table.add_column(cs.CLI_STATS_COL_NODE_TYPE, style=cs.Color.CYAN)
            node_table.add_column(cs.CLI_STATS_COL_COUNT, style=cs.Color.YELLOW, justify="right")

            for row in node_results:
                labels = row.get("labels", [])
                label = ":".join(labels) if labels else "Unknown"
                count = str(row.get("count", 0))
                node_table.add_row(label, f"{int(count):,}")

            node_table.add_section()
            node_table.add_row(
                style(cs.CLI_STATS_TOTAL_NODES, cs.Color.GREEN),
                style(f"{total_nodes:,}", cs.Color.GREEN),
            )

            app_context.console.print(node_table)
            app_context.console.print()

            rel_table = Table(
                title=style(cs.CLI_STATS_REL_TITLE, cs.Color.GREEN),
                show_header=True,
                header_style=f"bold {cs.Color.MAGENTA}",
            )
            rel_table.add_column(cs.CLI_STATS_COL_REL_TYPE, style=cs.Color.CYAN)
            rel_table.add_column(cs.CLI_STATS_COL_COUNT, style=cs.Color.YELLOW, justify="right")

            for row in rel_results:
                rel_type = str(row.get("type", "Unknown"))
                count = str(row.get("count", 0))
                rel_table.add_row(rel_type, f"{int(count):,}")

            rel_table.add_section()
            rel_table.add_row(
                style(cs.CLI_STATS_TOTAL_RELS, cs.Color.GREEN),
                style(f"{total_rels:,}", cs.Color.GREEN),
            )

            app_context.console.print(rel_table)

    except Exception as e:
        app_context.console.print(
            style(cs.CLI_ERR_STATS_FAILED.format(error=e), cs.Color.RED)
        )
        logger.exception(ls.STATS_ERROR.format(error=e))
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
