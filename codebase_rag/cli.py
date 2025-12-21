import asyncio
from pathlib import Path

import typer
from loguru import logger

from .config import settings
from .constants import (
    CLI_ERR_CONFIG,
    CLI_ERR_EXPORT_FAILED,
    CLI_ERR_INDEXING,
    CLI_ERR_LOAD_GRAPH,
    CLI_ERR_MCP_SERVER,
    CLI_ERR_ONLY_JSON,
    CLI_ERR_OUTPUT_REQUIRES_UPDATE,
    CLI_ERR_STARTUP,
    CLI_MSG_APP_TERMINATED,
    CLI_MSG_CLEANING_DB,
    CLI_MSG_CONNECTING_MEMGRAPH,
    CLI_MSG_EXPORTING_DATA,
    CLI_MSG_EXPORTING_TO,
    CLI_MSG_GRAPH_SUMMARY,
    CLI_MSG_GRAPH_UPDATED,
    CLI_MSG_HINT_TARGET_REPO,
    CLI_MSG_INDEXING_AT,
    CLI_MSG_INDEXING_DONE,
    CLI_MSG_MCP_TERMINATED,
    CLI_MSG_OPTIMIZATION_TERMINATED,
    CLI_MSG_OUTPUT_TO,
    CLI_MSG_UPDATING_GRAPH,
    Color,
)
from .graph_updater import GraphUpdater
from .main import (
    app_context,
    connect_memgraph,
    export_graph_to_file,
    main_async,
    main_optimize_async,
    style,
    update_model_settings,
)
from .parser_loader import load_parsers
from .services.protobuf_service import ProtobufFileIngestor
from .tools.language import cli as language_cli

app = typer.Typer(
    name="graph-code",
    help="An accurate Retrieval-Augmented Generation (RAG) system that analyzes "
    "multi-language codebases using Tree-sitter, builds comprehensive knowledge "
    "graphs, and enables natural language querying of codebase structure and "
    "relationships.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command(help="Start interactive chat session with your codebase")
def start(
    repo_path: str | None = typer.Option(
        None, "--repo-path", help="Path to the target repository for code retrieval"
    ),
    update_graph: bool = typer.Option(
        False,
        "--update-graph",
        help="Update the knowledge graph by parsing the repository",
    ),
    clean: bool = typer.Option(
        False,
        "--clean",
        help="Clean the database before updating (use when adding first repo)",
    ),
    output: str | None = typer.Option(
        None,
        "-o",
        "--output",
        help="Export graph to JSON file after updating (requires --update-graph)",
    ),
    orchestrator: str | None = typer.Option(
        None,
        "--orchestrator",
        help="Specify orchestrator as provider:model (e.g., ollama:llama3.2, openai:gpt-4, google:gemini-2.5-pro)",
    ),
    cypher: str | None = typer.Option(
        None,
        "--cypher",
        help="Specify cypher model as provider:model (e.g., ollama:codellama, google:gemini-2.5-flash)",
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Disable confirmation prompts for edit operations (YOLO mode)",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help="Number of buffered nodes/relationships before flushing to Memgraph",
    ),
) -> None:
    app_context.session.confirm_edits = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    if output and not update_graph:
        app_context.console.print(style(CLI_ERR_OUTPUT_REQUIRES_UPDATE, Color.RED))
        raise typer.Exit(1)

    update_model_settings(orchestrator, cypher)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    if update_graph:
        repo_to_update = Path(target_repo_path)
        app_context.console.print(
            style(CLI_MSG_UPDATING_GRAPH.format(path=repo_to_update), Color.GREEN)
        )

        with connect_memgraph(effective_batch_size) as ingestor:
            if clean:
                app_context.console.print(style(CLI_MSG_CLEANING_DB, Color.YELLOW))
                ingestor.clean_database()
            ingestor.ensure_constraints()

            parsers, queries = load_parsers()

            updater = GraphUpdater(ingestor, repo_to_update, parsers, queries)
            updater.run()

            if output:
                app_context.console.print(
                    style(CLI_MSG_EXPORTING_TO.format(path=output), Color.CYAN)
                )
                if not export_graph_to_file(ingestor, output):
                    raise typer.Exit(1)

        app_context.console.print(style(CLI_MSG_GRAPH_UPDATED, Color.GREEN))
        return

    try:
        asyncio.run(main_async(target_repo_path, effective_batch_size))
    except KeyboardInterrupt:
        app_context.console.print(style(CLI_MSG_APP_TERMINATED, Color.RED))
    except ValueError as e:
        app_context.console.print(style(CLI_ERR_STARTUP.format(error=e), Color.RED))


@app.command(help="Index codebase to protobuf files for offline use")
def index(
    repo_path: str | None = typer.Option(
        None, "--repo-path", help="Path to the target repository to index."
    ),
    output_proto_dir: str = typer.Option(
        ...,
        "-o",
        "--output-proto-dir",
        help="Required. Path to the output directory for the protobuf index file(s).",
    ),
    split_index: bool = typer.Option(
        False,
        "--split-index",
        help="Write index to separate nodes.bin and relationships.bin files.",
    ),
) -> None:
    target_repo_path = repo_path or settings.TARGET_REPO_PATH
    repo_to_index = Path(target_repo_path)

    app_context.console.print(
        style(CLI_MSG_INDEXING_AT.format(path=repo_to_index), Color.GREEN)
    )
    app_context.console.print(
        style(CLI_MSG_OUTPUT_TO.format(path=output_proto_dir), Color.CYAN)
    )

    try:
        ingestor = ProtobufFileIngestor(
            output_path=output_proto_dir, split_index=split_index
        )
        parsers, queries = load_parsers()
        updater = GraphUpdater(ingestor, repo_to_index, parsers, queries)

        updater.run()

        app_context.console.print(style(CLI_MSG_INDEXING_DONE, Color.GREEN))
    except Exception as e:
        app_context.console.print(style(CLI_ERR_INDEXING.format(error=e), Color.RED))
        logger.error("Indexing failed", exc_info=True)
        raise typer.Exit(1) from e


@app.command(help="Export knowledge graph from Memgraph to JSON file")
def export(
    output: str = typer.Option(
        ..., "-o", "--output", help="Output file path for the exported graph"
    ),
    format_json: bool = typer.Option(
        True, "--json/--no-json", help="Export in JSON format"
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help="Number of buffered nodes/relationships before flushing to Memgraph",
    ),
) -> None:
    if not format_json:
        app_context.console.print(style(CLI_ERR_ONLY_JSON, Color.RED))
        raise typer.Exit(1)

    app_context.console.print(style(CLI_MSG_CONNECTING_MEMGRAPH, Color.CYAN))

    effective_batch_size = settings.resolve_batch_size(batch_size)

    try:
        with connect_memgraph(effective_batch_size) as ingestor:
            app_context.console.print(style(CLI_MSG_EXPORTING_DATA, Color.CYAN))
            if not export_graph_to_file(ingestor, output):
                raise typer.Exit(1)

    except Exception as e:
        app_context.console.print(
            style(CLI_ERR_EXPORT_FAILED.format(error=e), Color.RED)
        )
        logger.error(f"Export error: {e}", exc_info=True)
        raise typer.Exit(1) from e


@app.command(help="AI-guided codebase optimization session")
def optimize(
    language: str = typer.Argument(
        ...,
        help="Programming language to optimize for (e.g., python, java, javascript, cpp)",
    ),
    repo_path: str | None = typer.Option(
        None, "--repo-path", help="Path to the repository to optimize"
    ),
    reference_document: str | None = typer.Option(
        None,
        "--reference-document",
        help="Path to reference document/book for optimization guidance",
    ),
    orchestrator: str | None = typer.Option(
        None,
        "--orchestrator",
        help="Specify orchestrator as provider:model (e.g., ollama:llama3.2, openai:gpt-4, google:gemini-2.5-pro)",
    ),
    cypher: str | None = typer.Option(
        None,
        "--cypher",
        help="Specify cypher model as provider:model (e.g., ollama:codellama, google:gemini-2.5-flash)",
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Disable confirmation prompts for edit operations (YOLO mode)",
    ),
    batch_size: int | None = typer.Option(
        None,
        "--batch-size",
        min=1,
        help="Number of buffered nodes/relationships before flushing to Memgraph",
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
        app_context.console.print(style(CLI_MSG_OPTIMIZATION_TERMINATED, Color.RED))
    except ValueError as e:
        app_context.console.print(style(CLI_ERR_STARTUP.format(error=e), Color.RED))


@app.command(name="mcp-server", help="Start the MCP server for Claude Code integration")
def mcp_server() -> None:
    try:
        from codebase_rag.mcp import main as mcp_main

        asyncio.run(mcp_main())
    except KeyboardInterrupt:
        app_context.console.print(style(CLI_MSG_MCP_TERMINATED, Color.RED))
    except ValueError as e:
        app_context.console.print(style(CLI_ERR_CONFIG.format(error=e), Color.RED))
        app_context.console.print(style(CLI_MSG_HINT_TARGET_REPO, Color.YELLOW))
    except Exception as e:
        app_context.console.print(style(CLI_ERR_MCP_SERVER.format(error=e), Color.RED))


@app.command(
    name="graph-loader", help="Load and display summary of exported graph JSON"
)
def graph_loader_command(
    graph_file: str = typer.Argument(..., help="Path to the exported graph JSON file"),
) -> None:
    from .graph_loader import load_graph

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        app_context.console.print(style(CLI_MSG_GRAPH_SUMMARY, Color.GREEN))
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
        app_context.console.print(style(CLI_ERR_LOAD_GRAPH.format(error=e), Color.RED))
        raise typer.Exit(1) from e


@app.command(
    name="language",
    help="Manage language grammars (add, remove, list)",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def language_command(ctx: typer.Context) -> None:
    language_cli(ctx.args, standalone_mode=False)


if __name__ == "__main__":
    app()
