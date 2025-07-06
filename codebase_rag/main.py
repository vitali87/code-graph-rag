import asyncio
import json
import os
import re
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

import typer
from loguru import logger
from prompt_toolkit import prompt
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import print_formatted_text
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from .config import settings
from .graph_updater import GraphUpdater, MemgraphIngestor
from .parser_loader import load_parsers
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from .tools.codebase_query import create_query_tool
from .tools.directory_lister import DirectoryLister, create_directory_lister_tool
from .tools.document_analyzer import DocumentAnalyzer, create_document_analyzer_tool
from .tools.file_editor import FileEditor, create_file_editor_tool
from .tools.file_reader import FileReader, create_file_reader_tool
from .tools.file_writer import FileWriter, create_file_writer_tool
from .tools.shell_command import ShellCommander, create_shell_command_tool

app = typer.Typer(
    name="graph-code",
    help="An accurate Retrieval-Augmented Generation (RAG) system that analyzes "
    "multi-language codebases using Tree-sitter, builds comprehensive knowledge "
    "graphs, and enables natural language querying of codebase structure and "
    "relationships.",
)
console = Console(width=None, force_terminal=True)


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

    for match in image_paths:
        original_path_str = match[0] if isinstance(match, tuple) else match
        original_path = Path(original_path_str)

        if not original_path.exists() or not original_path.is_file():
            logger.warning(f"Image path found, but does not exist: {original_path_str}")
            continue

        try:
            new_path = tmp_dir / f"{uuid.uuid4()}-{original_path.name}"
            shutil.copy(original_path, new_path)
            new_relative_path = os.path.relpath(new_path, project_root)

            # Replace the original path in the question with the new relative path
            updated_question = updated_question.replace(
                original_path_str, str(new_relative_path)
            )

            logger.info(f"Copied image to temporary path: {new_relative_path}")
        except Exception as e:
            logger.error(f"Failed to copy image to temporary directory: {e}")

    return updated_question


def get_multiline_input(prompt_text: str = "Ask a question") -> str:
    """Get multiline input from user with Ctrl+J to submit."""
    bindings = KeyBindings()

    @bindings.add("c-j")
    def submit(event: Any) -> None:
        """Submit the current input."""
        event.app.exit(result=event.app.current_buffer.text)

    @bindings.add("enter")
    def new_line(event: Any) -> None:
        """Insert a new line instead of submitting."""
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c")
    def keyboard_interrupt(event: Any) -> None:
        """Handle Ctrl+C."""
        event.app.exit(exception=KeyboardInterrupt)

    # Convert Rich markup to plain text using Rich's parser
    clean_prompt = Text.from_markup(prompt_text).plain

    # Display the colored prompt first
    print_formatted_text(HTML(
        f"<ansigreen><b>{clean_prompt}</b></ansigreen> <ansiyellow>(Press Ctrl+J to submit, Enter for new line)</ansiyellow>: "
    ))

    # Use simple prompt without formatting to avoid alignment issues
    result = prompt(
        "",
        multiline=True,
        key_bindings=bindings,
        wrap_lines=True,
    )
    if result is None:
        raise EOFError
    return result.strip()


async def run_chat_loop(rag_agent: Any, message_history: list[Any], project_root: Path) -> None:
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
                    get_multiline_input, "[bold cyan]Ask a question[/bold cyan]"
                )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            # Handle images in the question
            question = _handle_chat_images(question, project_root)

            with console.status("[bold green]Thinking...[/bold green]"):
                response = await rag_agent.run(
                    question, message_history=message_history
                )

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
    llm_provider: str | None,
    orchestrator_model: str | None,
    cypher_model: str | None,
) -> None:
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
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(graph_data, f, indent=2, ensure_ascii=False)

        console.print(
            f"[bold green]Graph exported successfully to: {output_path.absolute()}[/bold green]"
        )
        console.print(
            f"[bold cyan]Export contains {graph_data['metadata']['total_nodes']} nodes and {graph_data['metadata']['total_relationships']} relationships[/bold cyan]"
        )
        return True

    except Exception as e:
        console.print(f"[bold red]Failed to export graph: {e}[/bold red]")
        logger.error(f"Export error: {e}", exc_info=True)
        return False


def _initialize_services_and_agent(repo_path: str, ingestor: MemgraphIngestor) -> Any:
    """Initializes all services and creates the RAG agent."""
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

    query_tool = create_query_tool(ingestor, cypher_generator, console)
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
    return rag_agent


async def main_async(repo_path: str) -> None:
    """Initializes services and runs the main application loop."""
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

    # Clean up temp directory on startup
    project_root = Path(repo_path).resolve()
    tmp_dir = project_root / ".tmp"
    if tmp_dir.exists():
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir)
        else:
            tmp_dir.unlink()
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
        table.add_row("Local Model Endpoint", str(settings.LOCAL_MODEL_ENDPOINT))
    table.add_row("Target Repository", repo_path)
    console.print(table)

    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")
        console.print(
            Panel(
                "[bold yellow]Ask questions about your codebase graph. Type 'exit' or 'quit' to end.[/bold yellow]",
                border_style="yellow",
            )
        )

        rag_agent = _initialize_services_and_agent(repo_path, ingestor)
        await run_chat_loop(rag_agent, [], project_root)


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
    llm_provider: str | None = typer.Option(
        None, "--llm-provider", help="Choose the LLM provider: 'gemini' or 'local'"
    ),
    orchestrator_model: str | None = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: str | None = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
) -> None:
    """Starts the Codebase RAG CLI."""
    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    # Validate output option usage
    if output and not update_graph:
        console.print(
            "[bold red]Error: --output/-o option requires --update-graph to be specified.[/bold red]"
        )
        raise typer.Exit(1)

    _update_model_settings(llm_provider, orchestrator_model, cypher_model)

    if update_graph:

        repo_to_update = Path(target_repo_path)
        console.print(
            f"[bold green]Updating knowledge graph for: {repo_to_update}[/bold green]"
        )

        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
        ) as ingestor:
            if clean:
                console.print("[bold yellow]Cleaning database...[/bold yellow]")
                ingestor.clean_database()
            ingestor.ensure_constraints()
            
            # Load parsers and queries
            parsers, queries = load_parsers()
            
            updater = GraphUpdater(ingestor, repo_to_update, parsers, queries)
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
    output: str = typer.Option(
        ..., "-o", "--output", help="Output file path for the exported graph"
    ),
    format_json: bool = typer.Option(
        True, "--json/--no-json", help="Export in JSON format"
    ),
) -> None:
    """Export the current knowledge graph to a file."""
    if not format_json:
        console.print(
            "[bold red]Error: Currently only JSON format is supported.[/bold red]"
        )
        raise typer.Exit(1)

    console.print("[bold cyan]Connecting to Memgraph to export graph...[/bold cyan]")

    try:
        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
        ) as ingestor:
            console.print("[bold cyan]Exporting graph data...[/bold cyan]")
            if not _export_graph_to_file(ingestor, output):
                raise typer.Exit(1)

    except Exception as e:
        console.print(f"[bold red]Failed to export graph: {e}[/bold red]")
        logger.error(f"Export error: {e}", exc_info=True)
        raise typer.Exit(1) from e


async def run_optimization_loop(rag_agent: Any, message_history: list[Any], project_root: Path, language: str, reference_document: str | None = None) -> None:
    """Runs the optimization loop with the RAG agent."""
    console.print(f"[bold green]Starting {language} optimization session...[/bold green]")
    document_info = f" using the reference document: {reference_document}" if reference_document else ""
    console.print(
        Panel(
            f"[bold yellow]The agent will analyze your {language} codebase{document_info} and propose specific optimizations.\n"
            f"You'll be asked to approve each suggestion before implementation.\n"
            f"Type 'exit' or 'quit' to end the session.[/bold yellow]",
            border_style="yellow",
        )
    )

    # Initial optimization analysis

    instructions = [
        "Use your code retrieval and graph querying tools to understand the codebase structure",
        "Read relevant source files to identify optimization opportunities",
    ]
    if reference_document:
        instructions.append(f"Use the analyze_document tool to reference best practices from {reference_document}")

    instructions.extend([
        f"Reference established patterns and best practices for {language}",
        "Propose specific, actionable optimizations with file references",
        "Ask for my approval before implementing any changes",
        "Use your file editing tools to implement approved changes",
    ])

    numbered_instructions = "\n".join(f"{i+1}. {inst}" for i, inst in enumerate(instructions))

    initial_question = f"""
I want you to analyze my {language} codebase and propose specific optimizations based on best practices.

Please:
{numbered_instructions}

Start by analyzing the codebase structure and identifying the main areas that could benefit from optimization.
"""

    question = initial_question
    first_run = True

    while True:
        try:
            # If the last response was a confirmation request, use a confirm prompt
            if "[y/n]" in question:
                if Confirm.ask("Do you approve?"):
                    question = "yes"
                else:
                    question = "no"
                    console.print("[bold yellow]Operation cancelled.[/bold yellow]")
            elif not first_run:
                # Ask for user input on subsequent iterations
                question = await asyncio.to_thread(
                    get_multiline_input, "[bold cyan]Your response[/bold cyan]"
                )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            # Handle images in the question
            question = _handle_chat_images(question, project_root)

            with console.status("[bold green]Agent is analyzing codebase...[/bold green]"):
                response = await rag_agent.run(
                    question, message_history=message_history
                )

            # Store the agent's raw output to check for confirmation requests
            question = response.output
            markdown_response = Markdown(question)
            console.print(
                Panel(
                    markdown_response,
                    title="[bold green]Optimization Agent[/bold green]",
                    border_style="green",
                )
            )
            message_history.extend(response.new_messages())
            first_run = False

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


async def main_optimize_async(
    language: str,
    target_repo_path: str,
    reference_document: str | None = None,
    llm_provider: str | None = None,
    orchestrator_model: str | None = None,
    cypher_model: str | None = None,
) -> None:
    """Async wrapper for the optimization functionality."""
    project_root = Path(target_repo_path).resolve()

    _update_model_settings(llm_provider, orchestrator_model, cypher_model)

    console.print(f"[bold cyan]Initializing optimization session for {language} codebase: {project_root}[/bold cyan]")

    # Clean up temp directory on startup
    tmp_dir = project_root / ".tmp"
    if tmp_dir.exists():
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir)
        else:
            tmp_dir.unlink()
    tmp_dir.mkdir()

    # Display configuration
    table = Table(title="[bold green]Optimization Session Configuration[/bold green]")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Target Language", language)
    table.add_row("Repository Path", str(project_root))
    table.add_row("LLM Provider", settings.LLM_PROVIDER)
    if settings.LLM_PROVIDER == "gemini":
        table.add_row("Orchestrator Model", settings.GEMINI_MODEL_ID)
        table.add_row("Cypher Model", settings.MODEL_CYPHER_ID)
    else:
        table.add_row("Orchestrator Model", settings.LOCAL_ORCHESTRATOR_MODEL_ID)
        table.add_row("Cypher Model", settings.LOCAL_CYPHER_MODEL_ID)
    console.print(table)

    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")

        rag_agent = _initialize_services_and_agent(target_repo_path, ingestor)
        await run_optimization_loop(rag_agent, [], project_root, language, reference_document)


@app.command()
def optimize(
    language: str = typer.Argument(..., help="Programming language to optimize for (e.g., python, java, javascript, cpp)"),
    repo_path: str | None = typer.Option(
        None, "--repo-path", help="Path to the repository to optimize"
    ),
    reference_document: str | None = typer.Option(
        None, "--reference-document", help="Path to reference document/book for optimization guidance"
    ),
    llm_provider: str | None = typer.Option(
        None, "--llm-provider", help="Choose the LLM provider: 'gemini' or 'local'"
    ),
    orchestrator_model: str | None = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: str | None = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
) -> None:
    """Interactive codebase optimization using RAG agent with best practices guidance."""
    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    if not Path(target_repo_path).exists():
        console.print(f"[bold red]Error: Repository path '{target_repo_path}' does not exist.[/bold red]")
        raise typer.Exit(1)

    try:
        asyncio.run(main_optimize_async(
            language=language,
            target_repo_path=target_repo_path,
            reference_document=reference_document,
            llm_provider=llm_provider,
            orchestrator_model=orchestrator_model,
            cypher_model=cypher_model,
        ))
    except KeyboardInterrupt:
        console.print("\n[bold red]Optimization session terminated by user.[/bold red]")
    except Exception as e:
        console.print(f"[bold red]Failed to start optimization session: {e}[/bold red]")
        logger.error(f"Optimization error: {e}", exc_info=True)
        raise typer.Exit(1) from e


if __name__ == "__main__":
    app()
