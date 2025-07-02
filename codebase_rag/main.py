import asyncio
import sys
import typer
import json
import shutil
import re
import os
import uuid
from typing import Optional, List
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown
from pathlib import Path

from .config import settings
from .graph_updater import MemgraphIngestor, GraphUpdater
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.codebase_query import create_query_tool
from .tools.code_retrieval import create_code_retrieval_tool, CodeRetriever
from .tools.file_reader import create_file_reader_tool, FileReader
from .tools.file_writer import create_file_writer_tool, FileWriter
from .tools.file_editor import create_file_editor_tool, FileEditor
from .tools.shell_command import ShellCommander, create_shell_command_tool
from .tools.directory_lister import DirectoryLister, create_directory_lister_tool
from .tools.document_analyzer import DocumentAnalyzer, create_document_analyzer_tool 

from loguru import logger

app = typer.Typer(
    name="graph-code",
    help="An accurate Retrieval-Augmented Generation (RAG) system that analyzes "
    "multi-language codebases using Tree-sitter, builds comprehensive knowledge "
    "graphs, and enables natural language querying of codebase structure and "
    "relationships.",
)
console = Console()


def _handle_chat_images(question: str, project_root: Path) -> str:
    """
    Checks for image file paths in the question, copies them to a temporary
    directory, and replaces the path in the question.
    """
    # Find all potential absolute file paths with image extensions
    image_paths = re.findall(r"(/[^/ ]*?/.*\.(png|jpg|jpeg|gif))", question)
    if not image_paths:
        return question

    updated_question = question
    tmp_dir = project_root / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    for original_path_str in image_paths:
        original_path = Path(original_path_str)

        if not original_path.exists() or not original_path.is_file():
            logger.warning(f"Image path found, but does not exist: {original_path_str}")
            continue

        try:
            new_path = tmp_dir / f"{uuid.uuid4()}-{original_path.name}"
            shutil.copy(original_path, new_path)
            new_relative_path = os.path.relpath(new_path, project_root)
            
            # Replace the original path in the question with the new relative path
            updated_question = updated_question.replace(original_path_str, str(new_relative_path))
            
            logger.info(f"Copied image to temporary path: {new_relative_path}")
        except Exception as e:
            logger.error(f"Failed to copy image to temporary directory: {e}")
            
    return updated_question


async def run_chat_loop(rag_agent, message_history: List, project_root: Path):
    """Runs the main chat loop."""
    question = ""
    while True:
        try:
            # If the last response was a confirmation request, use a confirm prompt
            if "[y/n]" in question:
                if Confirm.ask("Do you approve?"):
                    question = "yes"
                else:
                    question = "no"
                    console.print("[bold yellow]Operation cancelled.[/bold yellow]")
            else:
                question = await asyncio.to_thread(
                    Prompt.ask, "[bold cyan]Ask a question[/bold cyan]"
                )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            # Handle images in the question
            question = _handle_chat_images(question, project_root)

            with console.status("[bold green]Thinking...[/bold green]"):
                response = await rag_agent.run(question, message_history=message_history)

            # Store the agent's raw output to check for confirmation requests
            question = response.output
            markdown_response = Markdown(question)
            console.print(
                Panel(
                    markdown_response,
                    title="[bold green]Final Answer[/bold green]",
                    border_style="green",
                )
            )
            message_history.extend(response.new_messages())

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


def _update_model_settings(
    llm_provider: Optional[str],
    orchestrator_model: Optional[str],
    cypher_model: Optional[str],
):
    """Update model settings based on command-line arguments."""
    if llm_provider:
        settings.LLM_PROVIDER = llm_provider

    provider = settings.LLM_PROVIDER
    if orchestrator_model:
        if provider == "gemini":
            settings.GEMINI_MODEL_ID = orchestrator_model
        else:
            settings.LOCAL_ORCHESTRATOR_MODEL_ID = orchestrator_model
    if cypher_model:
        if provider == "gemini":
            settings.MODEL_CYPHER_ID = cypher_model
        else:
            settings.LOCAL_CYPHER_MODEL_ID = cypher_model


def _export_graph_to_file(ingestor: MemgraphIngestor, output: str) -> bool:
    """
    Export graph data to a JSON file.
    
    Args:
        ingestor: The MemgraphIngestor instance to export from
        output: Output file path
        
    Returns:
        True if export was successful, False otherwise
    """

    
    try:
        graph_data = ingestor.export_graph_to_dict()
        output_path = Path(output)
        
        # Ensure the output directory exists
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Write JSON with proper formatting
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)
        
        console.print(f"[bold green]Graph exported successfully to: {output_path.absolute()}[/bold green]")
        console.print(f"[bold cyan]Export contains {graph_data['metadata']['total_nodes']} nodes and {graph_data['metadata']['total_relationships']} relationships[/bold cyan]")
        return True
        
    except Exception as e:
        console.print(f"[bold red]Failed to export graph: {e}[/bold red]")
        logger.error(f"Export error: {e}", exc_info=True)
        return False


async def main_async(repo_path: str):
    """Initializes services and runs the main application loop."""
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-DD-MM HH:mm:ss.SSS} | {message}")

    # Clean up temp directory on startup
    project_root = Path(repo_path).resolve()
    tmp_dir = project_root / ".tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir()

    table = Table(title="[bold green]Graph-Code Initializing...[/bold green]")
    table.add_column("Configuration", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("LLM Provider", settings.LLM_PROVIDER)
    if settings.LLM_PROVIDER == "gemini":
        table.add_row("Orchestrator Model", settings.GEMINI_MODEL_ID)
        table.add_row("Cypher Model", settings.MODEL_CYPHER_ID)
    else:
        table.add_row("Orchestrator Model", settings.LOCAL_ORCHESTRATOR_MODEL_ID)
        table.add_row("Cypher Model", settings.LOCAL_CYPHER_MODEL_ID)
        table.add_row("Local Model Endpoint", settings.LOCAL_MODEL_ENDPOINT)
    table.add_row("Target Repository", repo_path)
    console.print(table)

    with MemgraphIngestor(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")
        console.print(Panel(
            "[bold yellow]Ask questions about your codebase graph. Type 'exit' or 'quit' to end.[/bold yellow]",
            border_style="yellow"
        ))

        cypher_generator = CypherGenerator()
        code_retriever = CodeRetriever(project_root=repo_path, ingestor=ingestor)
        file_reader = FileReader(project_root=repo_path)
        file_writer = FileWriter(project_root=repo_path)
        file_editor = FileEditor(project_root=repo_path)
        shell_commander = ShellCommander(
            project_root=repo_path, timeout=settings.SHELL_COMMAND_TIMEOUT
        )
        directory_lister = DirectoryLister(project_root=repo_path)
        document_analyzer = DocumentAnalyzer(project_root=repo_path)

        query_tool = create_query_tool(ingestor, cypher_generator)
        code_tool = create_code_retrieval_tool(code_retriever)
        file_reader_tool = create_file_reader_tool(file_reader)
        file_writer_tool = create_file_writer_tool(file_writer)
        file_editor_tool = create_file_editor_tool(file_editor)
        shell_command_tool = create_shell_command_tool(shell_commander)
        directory_lister_tool = create_directory_lister_tool(directory_lister)
        document_analyzer_tool = create_document_analyzer_tool(document_analyzer)

        rag_agent = create_rag_orchestrator(
            tools=[
                query_tool,
                code_tool,
                file_reader_tool,
                file_writer_tool,
                file_editor_tool,
                shell_command_tool,
                directory_lister_tool,
                document_analyzer_tool,
            ]
        )

        await run_chat_loop(rag_agent, [], project_root)


@app.command()
def start(
    repo_path: Optional[str] = typer.Option(
        None, "--repo-path", help="Path to the target repository for code retrieval"
    ),
    show_repo: bool = typer.Option(
        False, "--show-repo", help="Show the repository being analyzed and exit"
    ),
    update_graph: bool = typer.Option(
        False, "--update-graph", help="Update the knowledge graph by parsing the repository"
    ),
    clean: bool = typer.Option(
        False, "--clean", help="Clean the database before updating (use when adding first repo)"
    ),
    output: Optional[str] = typer.Option(
        None, "-o", "--output", help="Export graph to JSON file after updating (requires --update-graph)"
    ),
    llm_provider: Optional[str] = typer.Option(
        None, "--llm-provider", help="Choose the LLM provider: 'gemini' or 'local'"
    ),
    orchestrator_model: Optional[str] = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: Optional[str] = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
):
    """Starts the Codebase RAG CLI."""
    target_repo_path = repo_path or settings.TARGET_REPO_PATH
    
    if show_repo:
        console.print(f"[bold green]Repository being analyzed:[/bold green] {target_repo_path}")
        raise typer.Exit()

    # Validate output option usage
    if output and not update_graph:
        console.print("[bold red]Error: --output/-o option requires --update-graph to be specified.[/bold red]")
        raise typer.Exit(1)
    
    _update_model_settings(llm_provider, orchestrator_model, cypher_model)

    if update_graph:
        
        repo_to_update = Path(target_repo_path)
        console.print(f"[bold green]Updating knowledge graph for: {repo_to_update}[/bold green]")

        with MemgraphIngestor(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT) as ingestor:
            if clean:
                console.print("[bold yellow]Cleaning database...[/bold yellow]")
                ingestor.clean_database()
            ingestor.ensure_constraints()
            updater = GraphUpdater(ingestor, repo_to_update)
            updater.run()
            
            # Export graph if output file specified
            if output:
                console.print(f"[bold cyan]Exporting graph to: {output}[/bold cyan]")
                if not _export_graph_to_file(ingestor, output):
                    raise typer.Exit(1)
        
        console.print("[bold green]Graph update completed![/bold green]")
        return

    try:
        asyncio.run(main_async(target_repo_path))
    except KeyboardInterrupt:
        console.print("\n[bold red]Application terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Startup Error: {e}[/bold red]")


@app.command()
def export(
    output: str = typer.Option(..., "-o", "--output", help="Output file path for the exported graph"),
    format_json: bool = typer.Option(True, "--json/--no-json", help="Export in JSON format"),
):
    """Export the current knowledge graph to a file."""
    if not format_json:
        console.print("[bold red]Error: Currently only JSON format is supported.[/bold red]")
        raise typer.Exit(1)

    console.print("[bold cyan]Connecting to Memgraph to export graph...[/bold cyan]")

    try:
        with MemgraphIngestor(host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT) as ingestor:
            console.print("[bold cyan]Exporting graph data...[/bold cyan]")
            if not _export_graph_to_file(ingestor, output):
                raise typer.Exit(1)

    except Exception as e:
        console.print(f"[bold red]Failed to export graph: {e}[/bold red]")
        logger.error(f"Export error: {e}", exc_info=True)
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
