import asyncio
import json
import re
import shlex
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

from .config import (
    EDIT_INDICATORS,
    EDIT_REQUEST_KEYWORDS,
    EDIT_TOOLS,
    ORANGE_STYLE,
    detect_provider_from_model,
    settings,
)
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

# Style constants
confirm_edits_globally = True

# Pre-compile regex patterns
_FILE_MODIFICATION_PATTERNS = [
    re.compile(
        r"(modified|updated|created|edited):\s*[\w/\\.-]+\.(py|js|ts|java|cpp|c|h|go|rs)"
    ),
    re.compile(
        r"file\s+[\w/\\.-]+\.(py|js|ts|java|cpp|c|h|go|rs)\s+(modified|updated|created|edited)"
    ),
    re.compile(r"writing\s+to\s+[\w/\\.-]+\.(py|js|ts|java|cpp|c|h|go|rs)"),
]


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

# Session logging
session_log_file = None
session_cancelled = False
# # Global flag to control edit confirmation
# confirm_edits = True


def init_session_log(project_root: Path) -> Path:
    """Initialize session log file."""
    global session_log_file
    log_dir = project_root / ".tmp"
    log_dir.mkdir(exist_ok=True)
    session_log_file = log_dir / f"session_{uuid.uuid4().hex[:8]}.log"
    with open(session_log_file, "w") as f:
        f.write("=== CODE-GRAPH RAG SESSION LOG ===\n\n")
    return session_log_file


def log_session_event(event: str) -> None:
    """Log an event to the session file."""
    global session_log_file
    if session_log_file:
        with open(session_log_file, "a") as f:
            f.write(f"{event}\n")


def get_session_context() -> str:
    """Get the full session context for cancelled operations."""
    global session_log_file
    if session_log_file and session_log_file.exists():
        content = Path(session_log_file).read_text()
        return f"\n\n[SESSION CONTEXT - Previous conversation in this session]:\n{content}\n[END SESSION CONTEXT]\n\n"
    return ""


def is_edit_operation_request(question: str) -> bool:
    """Check if the user's question/request would likely result in edit operations."""
    question_lower = question.lower()
    return any(keyword in question_lower for keyword in EDIT_REQUEST_KEYWORDS)


async def _handle_rejection(
    rag_agent: Any, message_history: list[Any], console: Console
) -> Any:
    """Handle user rejection of edits with agent acknowledgment."""
    rejection_message = "The user has rejected the changes that were made. Please acknowledge this and consider if any changes need to be reverted."

    with console.status("[bold yellow]Processing rejection...[/bold yellow]"):
        rejection_response = await run_with_cancellation(
            console,
            rag_agent.run(rejection_message, message_history=message_history),
        )

    if not (
        isinstance(rejection_response, dict) and rejection_response.get("cancelled")
    ):
        rejection_markdown = Markdown(rejection_response.output)
        console.print(
            Panel(
                rejection_markdown,
                title="[bold yellow]Response to Rejection[/bold yellow]",
                border_style="yellow",
            )
        )
        message_history.extend(rejection_response.new_messages())

    return rejection_response


def is_edit_operation_response(response_text: str) -> bool:
    """Enhanced check if the response contains edit operations that need confirmation."""
    response_lower = response_text.lower()

    # Check for tool usage
    tool_usage = any(tool in response_lower for tool in EDIT_TOOLS)

    # Check for content indicators
    content_indicators = any(
        indicator in response_lower for indicator in EDIT_INDICATORS
    )

    # Check for regex patterns
    pattern_match = any(
        pattern.search(response_lower) for pattern in _FILE_MODIFICATION_PATTERNS
    )

    return tool_usage or content_indicators or pattern_match


def _setup_common_initialization(repo_path: str) -> Path:
    """Common setup logic for both main and optimize functions."""
    # Logger initialization
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

    # Temporary directory cleanup
    project_root = Path(repo_path).resolve()
    tmp_dir = project_root / ".tmp"
    if tmp_dir.exists():
        if tmp_dir.is_dir():
            shutil.rmtree(tmp_dir)
        else:
            tmp_dir.unlink()
    tmp_dir.mkdir()

    return project_root


def _create_configuration_table(
    repo_path: str,
    title: str = "Graph-Code Initializing...",
    language: str | None = None,
) -> Table:
    """Create and return a configuration table."""
    table = Table(title=f"[bold green]{title}[/bold green]")
    table.add_column("Configuration", style="cyan")
    table.add_column("Value", style="magenta")

    # Add language row if provided (for optimization sessions)
    if language:
        table.add_row("Target Language", language)

    orchestrator_model = settings.active_orchestrator_model
    orchestrator_provider = detect_provider_from_model(orchestrator_model)
    table.add_row(
        "Orchestrator Model", f"{orchestrator_model} ({orchestrator_provider})"
    )

    cypher_model = settings.active_cypher_model
    cypher_provider = detect_provider_from_model(cypher_model)
    table.add_row("Cypher Model", f"{cypher_model} ({cypher_provider})")

    # Show local endpoint if any model is using local provider
    if orchestrator_provider == "local" or cypher_provider == "local":
        table.add_row("Local Model Endpoint", str(settings.LOCAL_MODEL_ENDPOINT))

    # Show edit confirmation status
    confirmation_status = (
        "Enabled" if confirm_edits_globally else "Disabled (YOLO Mode)"
    )
    table.add_row("Edit Confirmation", confirmation_status)
    table.add_row("Target Repository", repo_path)

    return table


async def run_optimization_loop(
    rag_agent: Any,
    message_history: list[Any],
    project_root: Path,
    language: str,
    reference_document: str | None = None,
) -> None:
    """Runs the optimization loop with proper confirmation handling."""
    global session_cancelled

    # Initialize session logging
    init_session_log(project_root)
    console.print(
        f"[bold green]Starting {language} optimization session...[/bold green]"
    )
    document_info = (
        f" using the reference document: {reference_document}"
        if reference_document
        else ""
    )
    console.print(
        Panel(
            f"[bold yellow]The agent will analyze your {language} codebase{document_info} and propose specific optimizations."
            f" You'll be asked to approve each suggestion before implementation."
            f" Type 'exit' or 'quit' to end the session.[/bold yellow]",
            border_style="yellow",
        )
    )

    # Initial optimization analysis
    instructions = [
        "Use your code retrieval and graph querying tools to understand the codebase structure",
        "Read relevant source files to identify optimization opportunities",
    ]
    if reference_document:
        instructions.append(
            f"Use the analyze_document tool to reference best practices from {reference_document}"
        )

    instructions.extend(
        [
            f"Reference established patterns and best practices for {language}",
            "Propose specific, actionable optimizations with file references",
            "IMPORTANT: Do not make any changes yet - just propose them and wait for approval",
            "After approval, use your file editing tools to implement the changes",
        ]
    )

    numbered_instructions = "\n".join(
        f"{i + 1}. {inst}" for i, inst in enumerate(instructions)
    )

    initial_question = f"""
I want you to analyze my {language} codebase and propose specific optimizations based on best practices.

Please:
{numbered_instructions}

Start by analyzing the codebase structure and identifying the main areas that could benefit from optimization.
Remember: Propose changes first, wait for my approval, then implement.
"""

    first_run = True
    question = initial_question

    while True:
        try:
            if not first_run:
                # Ask for user input on subsequent iterations
                question = await asyncio.to_thread(
                    get_multiline_input, "[bold cyan]Your response[/bold cyan]"
                )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            # Log user question
            log_session_event(f"USER: {question}")

            # If previous thinking was cancelled, add session context
            if session_cancelled:
                question_with_context = question + get_session_context()
                session_cancelled = False
            else:
                question_with_context = question

            # Handle images in the question
            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            with console.status(
                "[bold green]Agent is analyzing codebase... (Press Ctrl+C to cancel)[/bold green]"
            ):
                response = await run_with_cancellation(
                    console,
                    rag_agent.run(
                        question_with_context, message_history=message_history
                    ),
                )

                if isinstance(response, dict) and response.get("cancelled"):
                    log_session_event("ASSISTANT: [Analysis was cancelled]")
                    session_cancelled = True
                    continue

            # Display the response
            markdown_response = Markdown(response.output)
            console.print(
                Panel(
                    markdown_response,
                    title="[bold green]Optimization Agent[/bold green]",
                    border_style="green",
                )
            )

            # Check if confirmation is needed for edit operations
            if confirm_edits_globally and is_edit_operation_response(response.output):
                console.print(
                    "\n[bold yellow]⚠️  This optimization has performed file modifications.[/bold yellow]"
                )

                if not Confirm.ask(
                    "[bold cyan]Do you want to keep these optimizations?[/bold cyan]"
                ):
                    console.print(
                        "[bold red]❌ Optimizations rejected by user.[/bold red]"
                    )
                    await _handle_rejection(rag_agent, message_history, console)
                    first_run = False
                    continue
                else:
                    console.print(
                        "[bold green]✅ Optimizations approved by user.[/bold green]"
                    )

            # Log assistant response
            log_session_event(f"ASSISTANT: {response.output}")

            # Add the original response to message history only if not rejected
            message_history.extend(response.new_messages())
            first_run = False

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


async def run_with_cancellation(
    console: Console, coro: Any, timeout: float | None = None
) -> Any:
    """Run a coroutine with proper Ctrl+C cancellation that doesn't exit the program."""
    task = asyncio.create_task(coro)

    try:
        return await asyncio.wait_for(task, timeout=timeout) if timeout else await task
    except TimeoutError:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        console.print(
            f"\n[bold yellow]Operation timed out after {timeout} seconds.[/bold yellow]"
        )
        return {"cancelled": True, "timeout": True}
    except (asyncio.CancelledError, KeyboardInterrupt):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        console.print("\n[bold yellow]Thinking cancelled.[/bold yellow]")
        return {"cancelled": True}


def _handle_chat_images(question: str, project_root: Path) -> str:
    """
    Checks for image file paths in the question, copies them to a temporary
    directory, and replaces the path in the question.
    """
    # Use shlex to properly parse the question and handle escaped spaces
    try:
        tokens = shlex.split(question)
    except ValueError:
        # Fallback to simple split if shlex fails
        tokens = question.split()

    # Find image files in tokens
    image_extensions = (".png", ".jpg", ".jpeg", ".gif")
    image_files = [
        token
        for token in tokens
        if token.startswith("/") and token.lower().endswith(image_extensions)
    ]

    if not image_files:
        return question

    updated_question = question
    tmp_dir = project_root / ".tmp"
    tmp_dir.mkdir(exist_ok=True)

    for original_path_str in image_files:
        original_path = Path(original_path_str)

        if not original_path.exists() or not original_path.is_file():
            logger.warning(f"Image path found, but does not exist: {original_path_str}")
            continue

        try:
            new_path = tmp_dir / f"{uuid.uuid4()}-{original_path.name}"
            shutil.copy(original_path, new_path)
            new_relative_path = new_path.relative_to(project_root)

            # Find and replace all possible quoted/escaped versions of this path
            # Try different forms the path might appear in the original question
            path_variants = [
                # Backslash-escaped spaces: /path/with\ spaces.png
                original_path_str.replace(" ", r"\ "),
                # Single quoted: '/path/with spaces.png'
                f"'{original_path_str}'",
                # Double quoted: "/path/with spaces.png"
                f'"{original_path_str}"',
                # Unquoted: /path/with spaces.png
                original_path_str,
            ]

            # Try each variant and replace if found
            replaced = False
            for variant in path_variants:
                if variant in updated_question:
                    updated_question = updated_question.replace(
                        variant, str(new_relative_path)
                    )
                    replaced = True
                    break

            if not replaced:
                logger.warning(
                    f"Could not find original path in question for replacement: {original_path_str}"
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
    print_formatted_text(
        HTML(
            f"<ansigreen><b>{clean_prompt}</b></ansigreen> <ansiyellow>(Press Ctrl+J to submit, Enter for new line)</ansiyellow>: "
        )
    )

    # Use simple prompt without formatting to avoid alignment issues
    result = prompt(
        "",
        multiline=True,
        key_bindings=bindings,
        wrap_lines=True,
        style=ORANGE_STYLE,
    )
    if result is None:
        raise EOFError
    return result.strip()  # type: ignore[no-any-return]


async def run_chat_loop(
    rag_agent: Any, message_history: list[Any], project_root: Path
) -> None:
    """Runs the main chat loop with proper edit confirmation."""
    global session_cancelled

    # Initialize session logging
    init_session_log(project_root)

    while True:
        try:
            # Get user input
            question = await asyncio.to_thread(
                get_multiline_input, "[bold cyan]Ask a question[/bold cyan]"
            )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            # Log user question
            log_session_event(f"USER: {question}")

            # If previous thinking was cancelled, add session context
            if session_cancelled:
                question_with_context = question + get_session_context()
                session_cancelled = False
            else:
                question_with_context = question

            # Handle images in the question
            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            # Check if this might be an edit operation and warn user upfront
            might_edit = is_edit_operation_request(question)
            if confirm_edits_globally and might_edit:
                console.print(
                    "\n[bold yellow]⚠️  This request might result in file modifications.[/bold yellow]"
                )
                if not Confirm.ask(
                    "[bold cyan]Do you want to proceed with this request?[/bold cyan]"
                ):
                    console.print("[bold red]❌ Request cancelled by user.[/bold red]")
                    continue

            with console.status(
                "[bold green]Thinking... (Press Ctrl+C to cancel)[/bold green]"
            ):
                response = await run_with_cancellation(
                    console,
                    rag_agent.run(
                        question_with_context, message_history=message_history
                    ),
                )

                if isinstance(response, dict) and response.get("cancelled"):
                    log_session_event("ASSISTANT: [Thinking was cancelled]")
                    session_cancelled = True
                    continue

            # Display the response
            markdown_response = Markdown(response.output)
            console.print(
                Panel(
                    markdown_response,
                    title="[bold green]Assistant[/bold green]",
                    border_style="green",
                )
            )

            # Check if the response actually contains edit operations
            if confirm_edits_globally and is_edit_operation_response(response.output):
                console.print(
                    "\n[bold yellow]⚠️  The assistant has performed file modifications.[/bold yellow]"
                )

                if not Confirm.ask(
                    "[bold cyan]Do you want to keep these changes?[/bold cyan]"
                ):
                    console.print("[bold red]❌ User rejected the changes.[/bold red]")
                    await _handle_rejection(rag_agent, message_history, console)
                    continue
                else:
                    console.print(
                        "[bold green]✅ Changes accepted by user.[/bold green]"
                    )

            # Log assistant response
            log_session_event(f"ASSISTANT: {response.output}")

            # Add the response to message history only if it wasn't rejected
            message_history.extend(response.new_messages())

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


def _update_model_settings(
    orchestrator_model: str | None,
    cypher_model: str | None,
) -> None:
    """Update model settings based on command-line arguments."""
    # Set orchestrator model if provided
    if orchestrator_model:
        settings.set_orchestrator_model(orchestrator_model)

    # Set cypher model if provided
    if cypher_model:
        settings.set_cypher_model(cypher_model)


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
    # Validate settings once before initializing any LLM services
    settings.validate_for_usage()

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
    project_root = _setup_common_initialization(repo_path)

    table = _create_configuration_table(repo_path)
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
    orchestrator_model: str | None = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: str | None = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Disable confirmation prompts for edit operations (YOLO mode)",
    ),
) -> None:
    """Starts the Codebase RAG CLI."""
    global confirm_edits_globally

    # Set confirmation mode based on flag
    confirm_edits_globally = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    # Validate output option usage
    if output and not update_graph:
        console.print(
            "[bold red]Error: --output/-o option requires --update-graph to be specified.[/bold red]"
        )
        raise typer.Exit(1)

    _update_model_settings(orchestrator_model, cypher_model)

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


async def main_optimize_async(
    language: str,
    target_repo_path: str,
    reference_document: str | None = None,
    orchestrator_model: str | None = None,
    cypher_model: str | None = None,
) -> None:
    """Async wrapper for the optimization functionality."""
    project_root = _setup_common_initialization(target_repo_path)

    _update_model_settings(orchestrator_model, cypher_model)

    console.print(
        f"[bold cyan]Initializing optimization session for {language} codebase: {project_root}[/bold cyan]"
    )

    # Display configuration with language included
    table = _create_configuration_table(
        str(project_root), "Optimization Session Configuration", language
    )
    console.print(table)

    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")

        rag_agent = _initialize_services_and_agent(target_repo_path, ingestor)
        await run_optimization_loop(
            rag_agent, [], project_root, language, reference_document
        )


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
    orchestrator_model: str | None = typer.Option(
        None, "--orchestrator-model", help="Specify the orchestrator model ID"
    ),
    cypher_model: str | None = typer.Option(
        None, "--cypher-model", help="Specify the Cypher generator model ID"
    ),
    no_confirm: bool = typer.Option(
        False,
        "--no-confirm",
        help="Disable confirmation prompts for edit operations (YOLO mode)",
    ),
) -> None:
    """Optimize a codebase for a specific programming language."""
    global confirm_edits_globally

    # Set confirmation mode based on flag
    confirm_edits_globally = not no_confirm

    target_repo_path = repo_path or settings.TARGET_REPO_PATH

    try:
        asyncio.run(
            main_optimize_async(
                language,
                target_repo_path,
                reference_document,
                orchestrator_model,
                cypher_model,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[bold red]Optimization session terminated by user.[/bold red]")
    except ValueError as e:
        console.print(f"[bold red]Startup Error: {e}[/bold red]")


if __name__ == "__main__":
    app()
