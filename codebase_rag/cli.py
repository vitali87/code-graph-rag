import asyncio
from pathlib import Path

import typer
from loguru import logger
from rich.console import Console

from .config import settings
from .graph_updater import GraphUpdater
from .main import (
    _connect_memgraph,
    _export_graph_to_file,
    _update_model_settings,
    main_async,
    main_optimize_async,
    session_state,
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

console = Console(width=None, force_terminal=True)


@app.command()
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
    session_state.confirm_edits = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    if output and not update_graph:
        console.print(
            "[bold red]Error: --output/-o option requires --update-graph to be specified.[/bold red]"
        )
        raise typer.Exit(1)

    _update_model_settings(orchestrator, cypher)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    if update_graph:
        repo_to_update = Path(target_repo_path)
        console.print(
            f"[bold green]Updating knowledge graph for: {repo_to_update}[/bold green]"
        )

        with _connect_memgraph(effective_batch_size) as ingestor:
            if clean:
                console.print("[bold yellow]Cleaning database...[/bold yellow]")
                ingestor.clean_database()
            ingestor.ensure_constraints()

            parsers, queries = load_parsers()

            updater = GraphUpdater(ingestor, repo_to_update, parsers, queries)
            updater.run()

            if output:
                console.print(f"[bold cyan]Exporting graph to: {output}[/bold cyan]")
                if not _export_graph_to_file(ingestor, output):
                    raise typer.Exit(1)

        console.print("[bold green]Graph update completed![/bold green]")
        return

    try:
        asyncio.run(main_async(target_repo_path, effective_batch_size))
    except KeyboardInterrupt:
        console.print("\n[bold red]Application terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Startup Error: {e}[/bold red]")


@app.command()
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

    console.print(f"[bold green]Indexing codebase at: {repo_to_index}[/bold green]")
    console.print(
        f"[bold cyan]Output will be written to: {output_proto_dir}[/bold cyan]"
    )

    try:
        ingestor = ProtobufFileIngestor(
            output_path=output_proto_dir, split_index=split_index
        )
        parsers, queries = load_parsers()
        updater = GraphUpdater(ingestor, repo_to_index, parsers, queries)

        updater.run()

        console.print(
            "[bold green]Indexing process completed successfully![/bold green]"
        )
    except Exception as e:
        console.print(f"[bold red]An error occurred during indexing: {e}[/bold red]")
        logger.error("Indexing failed", exc_info=True)
        raise typer.Exit(1) from e


@app.command()
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
        console.print(
            "[bold red]Error: Currently only JSON format is supported.[/bold red]"
        )
        raise typer.Exit(1)

    console.print("[bold cyan]Connecting to Memgraph to export graph...[/bold cyan]")

    effective_batch_size = settings.resolve_batch_size(batch_size)

    try:
        with _connect_memgraph(effective_batch_size) as ingestor:
            console.print("[bold cyan]Exporting graph data...[/bold cyan]")
            if not _export_graph_to_file(ingestor, output):
                raise typer.Exit(1)

    except Exception as e:
        console.print(f"[bold red]Failed to export graph: {e}[/bold red]")
        logger.error(f"Export error: {e}", exc_info=True)
        raise typer.Exit(1) from e


@app.command()
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
    session_state.confirm_edits = not no_confirm

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
        console.print("\n[bold red]Optimization session terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Startup Error: {e}[/bold red]")


@app.command(name="mcp-server", help="Start the MCP server for Claude Code integration")
def mcp_server() -> None:
    try:
        from codebase_rag.mcp import main as mcp_main

        asyncio.run(mcp_main())
    except KeyboardInterrupt:
        console.print("\n[bold red]MCP server terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Configuration Error: {e}[/bold red]")
        console.print(
            "\n[yellow]Hint: Make sure TARGET_REPO_PATH environment variable is set.[/yellow]"
        )
    except Exception as e:
        console.print(f"[bold red]MCP Server Error: {e}[/bold red]")


@app.command(name="graph-loader")
def graph_loader_command(
    graph_file: str = typer.Argument(..., help="Path to the exported graph JSON file"),
) -> None:
    from .graph_loader import load_graph

    try:
        graph = load_graph(graph_file)
        summary = graph.summary()

        console.print("[bold green]Graph Summary:[/bold green]")
        console.print(f"  Total nodes: {summary['total_nodes']}")
        console.print(f"  Total relationships: {summary['total_relationships']}")
        console.print(f"  Node types: {list(summary['node_labels'].keys())}")
        console.print(
            f"  Relationship types: {list(summary['relationship_types'].keys())}"
        )
        console.print(f"  Exported at: {summary['metadata']['exported_at']}")

    except Exception as e:
        console.print(f"[bold red]Failed to load graph: {e}[/bold red]")
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
