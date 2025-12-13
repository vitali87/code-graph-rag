import asyncio
import difflib
import json
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
from pydantic_ai import DeferredToolRequests, DeferredToolResults, ToolDenied
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .config import (
    ORANGE_STYLE,
    settings,
)
from .graph_updater import GraphUpdater
from .parser_loader import load_parsers
from .services import QueryProtocol
from .services.graph_service import MemgraphIngestor
from .services.llm import CypherGenerator, create_rag_orchestrator
from .services.protobuf_service import ProtobufFileIngestor
from .tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from .tools.codebase_query import create_query_tool
from .tools.directory_lister import DirectoryLister, create_directory_lister_tool
from .tools.document_analyzer import DocumentAnalyzer, create_document_analyzer_tool
from .tools.file_editor import FileEditor, create_file_editor_tool
from .tools.file_reader import FileReader, create_file_reader_tool
from .tools.file_writer import FileWriter, create_file_writer_tool
from .tools.language import cli as language_cli
from .tools.semantic_search import (
    create_get_function_source_tool,
    create_semantic_search_tool,
)
from .tools.shell_command import ShellCommander, create_shell_command_tool

confirm_edits_globally = True

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

session_log_file = None
session_cancelled = False


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


def _display_tool_call_diff(
    tool_name: str, tool_args: dict[str, Any], file_path: str | None = None
) -> None:
    if tool_name == "replace_code_surgically":
        target = tool_args.get("target_code", "")
        replacement = tool_args.get("replacement_code", "")
        path = tool_args.get("file_path", file_path or "file")

        console.print(f"\n[bold cyan]File: {path}[/bold cyan]")
        console.print("[dim]" + "─" * 60 + "[/dim]")

        diff = difflib.unified_diff(
            target.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
            lineterm="",
        )

        for line in diff:
            line = line.rstrip("\n")
            if line.startswith("+++") or line.startswith("---"):
                console.print(f"[dim]{line}[/dim]")
            elif line.startswith("@@"):
                console.print(f"[cyan]{line}[/cyan]")
            elif line.startswith("+"):
                console.print(f"[green]{line}[/green]")
            elif line.startswith("-"):
                console.print(f"[red]{line}[/red]")
            else:
                console.print(line)

        console.print("[dim]" + "─" * 60 + "[/dim]")

    elif tool_name == "create_new_file":
        path = tool_args.get("file_path", "")
        content = tool_args.get("content", "")

        console.print(f"\n[bold cyan]New file: {path}[/bold cyan]")
        console.print("[dim]" + "─" * 60 + "[/dim]")

        for line in content.splitlines():
            console.print(f"[green]+ {line}[/green]")

        console.print("[dim]" + "─" * 60 + "[/dim]")

    elif tool_name == "execute_shell_command":
        command = tool_args.get("command", "")
        console.print("\n[bold cyan]Shell command:[/bold cyan]")
        console.print(f"[yellow]$ {command}[/yellow]")

    else:
        console.print(f"    Arguments: {json.dumps(tool_args, indent=2)}")


def _setup_common_initialization(repo_path: str) -> Path:
    """Common setup logic for both main and optimize functions."""
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

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

    if language:
        table.add_row("Target Language", language)

    orchestrator_config = settings.active_orchestrator_config
    table.add_row(
        "Orchestrator Model",
        f"{orchestrator_config.model_id} ({orchestrator_config.provider})",
    )

    cypher_config = settings.active_cypher_config
    table.add_row(
        "Cypher Model", f"{cypher_config.model_id} ({cypher_config.provider})"
    )

    orch_endpoint = (
        orchestrator_config.endpoint
        if orchestrator_config.provider == "ollama"
        else None
    )
    cypher_endpoint = (
        cypher_config.endpoint if cypher_config.provider == "ollama" else None
    )

    if orch_endpoint and cypher_endpoint and orch_endpoint == cypher_endpoint:
        table.add_row("Ollama Endpoint", orch_endpoint)
    else:
        if orch_endpoint:
            table.add_row("Ollama Endpoint (Orchestrator)", orch_endpoint)
        if cypher_endpoint:
            table.add_row("Ollama Endpoint (Cypher)", cypher_endpoint)

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
            f"[bold yellow]The agent will analyze your codebase{document_info} and propose specific optimizations."
            f" You'll be asked to approve each suggestion before implementation."
            f" Type 'exit' or 'quit' to end the session.[/bold yellow]",
            border_style="yellow",
        )
    )

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
                question = await asyncio.to_thread(
                    get_multiline_input, "[bold cyan]Your response[/bold cyan]"
                )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            log_session_event(f"USER: {question}")

            if session_cancelled:
                question_with_context = question + get_session_context()
                session_cancelled = False
            else:
                question_with_context = question

            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            deferred_results: DeferredToolResults | None = None

            while True:
                with console.status(
                    "[bold green]Agent is analyzing codebase... (Press Ctrl+C to cancel)[/bold green]"
                ):
                    response = await run_with_cancellation(
                        console,
                        rag_agent.run(
                            question_with_context,
                            message_history=message_history,
                            deferred_tool_results=deferred_results,
                        ),
                    )

                    if isinstance(response, dict) and response.get("cancelled"):
                        log_session_event("ASSISTANT: [Analysis was cancelled]")
                        session_cancelled = True
                        break

                if isinstance(response.output, DeferredToolRequests):
                    requests = response.output
                    deferred_results = DeferredToolResults()

                    for call in requests.approvals:
                        tool_args = call.args_as_dict()
                        console.print(
                            f"\n[bold yellow]⚠️  Tool '{call.tool_name}' requires approval:[/bold yellow]"
                        )
                        _display_tool_call_diff(call.tool_name, tool_args)

                        if confirm_edits_globally:
                            if Confirm.ask(
                                "[bold cyan]Do you approve this optimization?[/bold cyan]"
                            ):
                                deferred_results.approvals[call.tool_call_id] = True
                            else:
                                feedback = Prompt.ask(
                                    "[bold yellow]Feedback (why rejected, or press Enter to skip)[/bold yellow]",
                                    default="",
                                )
                                denial_msg = (
                                    feedback.strip()
                                    if feedback.strip()
                                    else "User rejected this optimization without feedback"
                                )
                                deferred_results.approvals[call.tool_call_id] = (
                                    ToolDenied(denial_msg)
                                )
                        else:
                            deferred_results.approvals[call.tool_call_id] = True

                    message_history.extend(response.new_messages())
                    continue

                markdown_response = Markdown(response.output)
                console.print(
                    Panel(
                        markdown_response,
                        title="[bold green]Optimization Agent[/bold green]",
                        border_style="green",
                    )
                )

                log_session_event(f"ASSISTANT: {response.output}")
                message_history.extend(response.new_messages())
                break

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
    try:
        tokens = shlex.split(question)
    except ValueError:
        tokens = question.split()

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

            path_variants = [
                original_path_str.replace(" ", r"\ "),
                f"'{original_path_str}'",
                f'"{original_path_str}"',
                original_path_str,
            ]

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

    clean_prompt = Text.from_markup(prompt_text).plain

    print_formatted_text(
        HTML(
            f"<ansigreen><b>{clean_prompt}</b></ansigreen> <ansiyellow>(Press Ctrl+J to submit, Enter for new line)</ansiyellow>: "
        )
    )

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
    global session_cancelled

    init_session_log(project_root)

    while True:
        try:
            question = await asyncio.to_thread(
                get_multiline_input, "[bold cyan]Ask a question[/bold cyan]"
            )

            if question.lower() in ["exit", "quit"]:
                break
            if not question.strip():
                continue

            log_session_event(f"USER: {question}")

            if session_cancelled:
                question_with_context = question + get_session_context()
                session_cancelled = False
            else:
                question_with_context = question

            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            deferred_results: DeferredToolResults | None = None

            while True:
                with console.status(
                    "[bold green]Thinking... (Press Ctrl+C to cancel)[/bold green]"
                ):
                    response = await run_with_cancellation(
                        console,
                        rag_agent.run(
                            question_with_context,
                            message_history=message_history,
                            deferred_tool_results=deferred_results,
                        ),
                    )

                    if isinstance(response, dict) and response.get("cancelled"):
                        log_session_event("ASSISTANT: [Thinking was cancelled]")
                        session_cancelled = True
                        break

                if isinstance(response.output, DeferredToolRequests):
                    requests = response.output
                    deferred_results = DeferredToolResults()

                    for call in requests.approvals:
                        tool_args = call.args_as_dict()
                        console.print(
                            f"\n[bold yellow]⚠️  Tool '{call.tool_name}' requires approval:[/bold yellow]"
                        )
                        _display_tool_call_diff(call.tool_name, tool_args)

                        if confirm_edits_globally:
                            if Confirm.ask(
                                "[bold cyan]Do you approve this change?[/bold cyan]"
                            ):
                                deferred_results.approvals[call.tool_call_id] = True
                            else:
                                feedback = Prompt.ask(
                                    "[bold yellow]Feedback (why rejected, or press Enter to skip)[/bold yellow]",
                                    default="",
                                )
                                denial_msg = (
                                    feedback.strip()
                                    if feedback.strip()
                                    else "User rejected this change without feedback"
                                )
                                deferred_results.approvals[call.tool_call_id] = (
                                    ToolDenied(denial_msg)
                                )
                        else:
                            deferred_results.approvals[call.tool_call_id] = True

                    message_history.extend(response.new_messages())
                    continue

                markdown_response = Markdown(response.output)
                console.print(
                    Panel(
                        markdown_response,
                        title="[bold green]Assistant[/bold green]",
                        border_style="green",
                    )
                )

                log_session_event(f"ASSISTANT: {response.output}")
                message_history.extend(response.new_messages())
                break

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


def _update_single_model_setting(role: str, model_string: str) -> None:
    """Update a single model setting (orchestrator or cypher)."""
    provider, model = settings.parse_model_string(model_string)

    if role == "orchestrator":
        current_config = settings.active_orchestrator_config
        set_method = settings.set_orchestrator
    else:
        current_config = settings.active_cypher_config
        set_method = settings.set_cypher

    kwargs = {
        "api_key": current_config.api_key,
        "endpoint": current_config.endpoint,
        "project_id": current_config.project_id,
        "region": current_config.region,
        "provider_type": current_config.provider_type,
        "thinking_budget": current_config.thinking_budget,
        "service_account_file": current_config.service_account_file,
    }

    if provider == "ollama" and not kwargs["endpoint"]:
        kwargs["endpoint"] = str(settings.LOCAL_MODEL_ENDPOINT)
        kwargs["api_key"] = "ollama"

    set_method(provider, model, **kwargs)


def _update_model_settings(
    orchestrator: str | None,
    cypher: str | None,
) -> None:
    """Update model settings based on command-line arguments."""
    if orchestrator:
        _update_single_model_setting("orchestrator", orchestrator)
    if cypher:
        _update_single_model_setting("cypher", cypher)


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

        output_path.parent.mkdir(parents=True, exist_ok=True)

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


def _initialize_services_and_agent(repo_path: str, ingestor: QueryProtocol) -> Any:
    """Initializes all services and creates the RAG agent."""
    from .providers.base import get_provider

    def _validate_provider_config(role: str, config: Any) -> None:
        """Validate a single provider configuration."""
        try:
            provider = get_provider(
                config.provider,
                api_key=config.api_key,
                endpoint=config.endpoint,
                project_id=config.project_id,
                region=config.region,
                provider_type=config.provider_type,
                thinking_budget=config.thinking_budget,
                service_account_file=config.service_account_file,
            )
            provider.validate_config()
        except Exception as e:
            raise ValueError(f"{role.title()} configuration error: {e}") from e

    _validate_provider_config("orchestrator", settings.active_orchestrator_config)
    _validate_provider_config("cypher", settings.active_cypher_config)

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
    semantic_search_tool = create_semantic_search_tool()
    function_source_tool = create_get_function_source_tool()

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
            semantic_search_tool,
            function_source_tool,
        ]
    )
    return rag_agent


async def main_async(repo_path: str, batch_size: int) -> None:
    """Initializes services and runs the main application loop."""
    project_root = _setup_common_initialization(repo_path)

    table = _create_configuration_table(repo_path)
    console.print(table)

    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=batch_size,
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
    """Starts the Codebase RAG CLI."""
    global confirm_edits_globally

    confirm_edits_globally = not no_confirm

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

        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST,
            port=settings.MEMGRAPH_PORT,
            batch_size=effective_batch_size,
        ) as ingestor:
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
    """Parses a codebase and creates a portable binary index file."""
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
        raise typer.Exit(1)


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
    """Export the current knowledge graph to a file."""
    if not format_json:
        console.print(
            "[bold red]Error: Currently only JSON format is supported.[/bold red]"
        )
        raise typer.Exit(1)

    console.print("[bold cyan]Connecting to Memgraph to export graph...[/bold cyan]")

    effective_batch_size = settings.resolve_batch_size(batch_size)

    try:
        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST,
            port=settings.MEMGRAPH_PORT,
            batch_size=effective_batch_size,
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
    orchestrator: str | None = None,
    cypher: str | None = None,
    batch_size: int | None = None,
) -> None:
    """Async wrapper for the optimization functionality."""
    project_root = _setup_common_initialization(target_repo_path)

    _update_model_settings(orchestrator, cypher)

    console.print(
        f"[bold cyan]Initializing optimization session for {language} codebase: {project_root}[/bold cyan]"
    )

    table = _create_configuration_table(
        str(project_root), "Optimization Session Configuration", language
    )
    console.print(table)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=effective_batch_size,
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
    """Optimize a codebase for a specific programming language."""
    global confirm_edits_globally

    confirm_edits_globally = not no_confirm

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


@app.command(name="mcp-server")
def mcp_server() -> None:
    """Start the MCP (Model Context Protocol) server.

    This command starts an MCP server that exposes code-graph-rag's capabilities
    to MCP clients like Claude Code. The server runs on stdio transport and requires
    the TARGET_REPO_PATH environment variable to be set to the target repository.

    Usage:
        graph-code mcp-server

    Environment Variables:
        TARGET_REPO_PATH: Path to the target repository (required)

    For Claude Code integration:
        claude mcp add --transport stdio graph-code \\
          --env TARGET_REPO_PATH=/path/to/your/project \\
          -- uv run --directory /path/to/code-graph-rag graph-code mcp-server
    """
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
    """Load and display summary of an exported graph file."""
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
        raise typer.Exit(1)


@app.command(
    name="language",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def language_command(ctx: typer.Context) -> None:
    """Manage language grammars (add, remove, list).

    Examples:
        cgr language add-grammar python
        cgr language list-languages
        cgr language remove-language python
    """
    language_cli(ctx.args, standalone_mode=False)


if __name__ == "__main__":
    app()
