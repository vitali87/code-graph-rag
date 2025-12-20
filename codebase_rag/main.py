import asyncio
import difflib
import json
import shlex
import shutil
import sys
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING

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

from .config import CHAT_LOOP_CONFIG, OPTIMIZATION_LOOP_CONFIG, ORANGE_STYLE, settings
from .constants import (
    EXIT_COMMANDS,
    HORIZONTAL_SEPARATOR,
    IMAGE_EXTENSIONS,
    SESSION_LOG_HEADER,
    SESSION_LOG_PREFIX,
    TMP_DIR,
    ModelRole,
    Provider,
    ToolName,
)
from .models import AgentLoopConfig, SessionState
from .services import QueryProtocol
from .services.graph_service import MemgraphIngestor
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from .tools.codebase_query import create_query_tool
from .tools.directory_lister import DirectoryLister, create_directory_lister_tool
from .tools.document_analyzer import DocumentAnalyzer, create_document_analyzer_tool
from .tools.file_editor import FileEditor, create_file_editor_tool
from .tools.file_reader import FileReader, create_file_reader_tool
from .tools.file_writer import FileWriter, create_file_writer_tool
from .tools.semantic_search import (
    create_get_function_source_tool,
    create_semantic_search_tool,
)
from .tools.shell_command import ShellCommander, create_shell_command_tool
from .types_defs import CancelledResult, ToolArgValue

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage

    from .config import ModelConfig

session_state = SessionState()

console = Console(width=None, force_terminal=True)


def init_session_log(project_root: Path) -> Path:
    log_dir = project_root / TMP_DIR
    log_dir.mkdir(exist_ok=True)
    session_state.log_file = log_dir / f"{SESSION_LOG_PREFIX}{uuid.uuid4().hex[:8]}.log"
    with open(session_state.log_file, "w") as f:
        f.write(SESSION_LOG_HEADER)
    return session_state.log_file


def log_session_event(event: str) -> None:
    if session_state.log_file:
        with open(session_state.log_file, "a") as f:
            f.write(f"{event}\n")


def get_session_context() -> str:
    if session_state.log_file and session_state.log_file.exists():
        content = session_state.log_file.read_text()
        return f"\n\n[SESSION CONTEXT - Previous conversation in this session]:\n{content}\n[END SESSION CONTEXT]\n\n"
    return ""


def _print_unified_diff(target: str, replacement: str, path: str) -> None:
    separator = f"[dim]{HORIZONTAL_SEPARATOR}[/dim]"
    console.print(f"\n[bold cyan]File: {path}[/bold cyan]")
    console.print(separator)

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

    console.print(separator)


def _print_new_file_content(path: str, content: str) -> None:
    separator = f"[dim]{HORIZONTAL_SEPARATOR}[/dim]"
    console.print(f"\n[bold cyan]New file: {path}[/bold cyan]")
    console.print(separator)

    for line in content.splitlines():
        console.print(f"[green]+ {line}[/green]")

    console.print(separator)


def _display_tool_call_diff(
    tool_name: str, tool_args: dict[str, ToolArgValue], file_path: str | None = None
) -> None:
    match tool_name:
        case ToolName.REPLACE_CODE:
            target = str(tool_args.get("target_code", ""))
            replacement = str(tool_args.get("replacement_code", ""))
            path = str(tool_args.get("file_path", file_path or "file"))
            _print_unified_diff(target, replacement, path)

        case ToolName.CREATE_FILE:
            path = str(tool_args.get("file_path", ""))
            content = str(tool_args.get("content", ""))
            _print_new_file_content(path, content)

        case ToolName.SHELL_COMMAND:
            command = tool_args.get("command", "")
            console.print("\n[bold cyan]Shell command:[/bold cyan]")
            console.print(f"[yellow]$ {command}[/yellow]")

        case _:
            console.print(f"    Arguments: {json.dumps(tool_args, indent=2)}")


def _process_tool_approvals(
    requests: DeferredToolRequests, approval_prompt: str, denial_default: str
) -> DeferredToolResults:
    deferred_results = DeferredToolResults()

    for call in requests.approvals:
        tool_args = call.args_as_dict()
        console.print(
            f"\n[bold yellow]⚠️  Tool '{call.tool_name}' requires approval:[/bold yellow]"
        )
        _display_tool_call_diff(call.tool_name, tool_args)

        if session_state.confirm_edits:
            if Confirm.ask(f"[bold cyan]{approval_prompt}[/bold cyan]"):
                deferred_results.approvals[call.tool_call_id] = True
            else:
                feedback = Prompt.ask(
                    "[bold yellow]Feedback (why rejected, or press Enter to skip)[/bold yellow]",
                    default="",
                )
                denial_msg = feedback.strip() or denial_default
                deferred_results.approvals[call.tool_call_id] = ToolDenied(denial_msg)
        else:
            deferred_results.approvals[call.tool_call_id] = True

    return deferred_results


def _setup_common_initialization(repo_path: str) -> Path:
    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

    project_root = Path(repo_path).resolve()
    tmp_dir = project_root / TMP_DIR
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
        if orchestrator_config.provider == Provider.OLLAMA
        else None
    )
    cypher_endpoint = (
        cypher_config.endpoint if cypher_config.provider == Provider.OLLAMA else None
    )

    if orch_endpoint and cypher_endpoint and orch_endpoint == cypher_endpoint:
        table.add_row("Ollama Endpoint", orch_endpoint)
    else:
        if orch_endpoint:
            table.add_row("Ollama Endpoint (Orchestrator)", orch_endpoint)
        if cypher_endpoint:
            table.add_row("Ollama Endpoint (Cypher)", cypher_endpoint)

    confirmation_status = (
        "Enabled" if session_state.confirm_edits else "Disabled (YOLO Mode)"
    )
    table.add_row("Edit Confirmation", confirmation_status)
    table.add_row("Target Repository", repo_path)

    return table


async def run_optimization_loop(
    rag_agent: "Agent[None, str | DeferredToolRequests]",
    message_history: list["ModelMessage"],
    project_root: Path,
    language: str,
    reference_document: str | None = None,
) -> None:
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

            if question.lower() in EXIT_COMMANDS:
                break
            if not question.strip():
                continue

            log_session_event(f"USER: {question}")

            if session_state.cancelled:
                question_with_context = question + get_session_context()
                session_state.reset_cancelled()
            else:
                question_with_context = question

            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            await _run_agent_response_loop(
                rag_agent,
                message_history,
                question_with_context,
                OPTIMIZATION_LOOP_CONFIG,
            )

            first_run = False

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


async def run_with_cancellation[T](
    console: Console, coro: Coroutine[None, None, T], timeout: float | None = None
) -> T | CancelledResult:
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
        return CancelledResult(cancelled=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        console.print("\n[bold yellow]Thinking cancelled.[/bold yellow]")
        return CancelledResult(cancelled=True)


async def _run_agent_response_loop(
    rag_agent: "Agent[None, str | DeferredToolRequests]",
    message_history: list["ModelMessage"],
    question_with_context: str,
    config: AgentLoopConfig,
) -> None:
    deferred_results: DeferredToolResults | None = None

    while True:
        with console.status(config.status_message):
            response = await run_with_cancellation(
                console,
                rag_agent.run(
                    question_with_context,
                    message_history=message_history,
                    deferred_tool_results=deferred_results,
                ),
            )

        if isinstance(response, CancelledResult):
            log_session_event(config.cancelled_log)
            session_state.cancelled = True
            break

        if isinstance(response.output, DeferredToolRequests):
            deferred_results = _process_tool_approvals(
                response.output,
                config.approval_prompt,
                config.denial_default,
            )
            message_history.extend(response.new_messages())
            continue

        output_text = response.output
        if not isinstance(output_text, str):
            continue
        markdown_response = Markdown(output_text)
        console.print(
            Panel(
                markdown_response,
                title=config.panel_title,
                border_style="green",
            )
        )

        log_session_event(f"ASSISTANT: {output_text}")
        message_history.extend(response.new_messages())
        break


def _handle_chat_images(question: str, project_root: Path) -> str:
    try:
        tokens = shlex.split(question)
    except ValueError:
        tokens = question.split()

    image_files = [
        token
        for token in tokens
        if token.startswith("/") and token.lower().endswith(IMAGE_EXTENSIONS)
    ]

    if not image_files:
        return question

    updated_question = question
    tmp_dir = project_root / TMP_DIR
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
    bindings = KeyBindings()

    @bindings.add("c-j")
    def submit(event: "KeyPressEvent") -> None:
        event.app.exit(result=event.app.current_buffer.text)

    @bindings.add("enter")
    def new_line(event: "KeyPressEvent") -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add("c-c")
    def keyboard_interrupt(event: "KeyPressEvent") -> None:
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
    stripped: str = result.strip()
    return stripped


async def run_chat_loop(
    rag_agent: "Agent[None, str | DeferredToolRequests]",
    message_history: list["ModelMessage"],
    project_root: Path,
) -> None:
    init_session_log(project_root)

    while True:
        try:
            question = await asyncio.to_thread(
                get_multiline_input, "[bold cyan]Ask a question[/bold cyan]"
            )

            if question.lower() in EXIT_COMMANDS:
                break
            if not question.strip():
                continue

            log_session_event(f"USER: {question}")

            if session_state.cancelled:
                question_with_context = question + get_session_context()
                session_state.reset_cancelled()
            else:
                question_with_context = question

            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            await _run_agent_response_loop(
                rag_agent, message_history, question_with_context, CHAT_LOOP_CONFIG
            )

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error("An unexpected error occurred: {}", e, exc_info=True)
            console.print(f"[bold red]An unexpected error occurred: {e}[/bold red]")


def _update_single_model_setting(role: ModelRole, model_string: str) -> None:
    provider, model = settings.parse_model_string(model_string)

    match role:
        case ModelRole.ORCHESTRATOR:
            current_config = settings.active_orchestrator_config
            set_method = settings.set_orchestrator
        case ModelRole.CYPHER:
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

    if provider == Provider.OLLAMA and not kwargs["endpoint"]:
        kwargs["endpoint"] = str(settings.LOCAL_MODEL_ENDPOINT)
        kwargs["api_key"] = Provider.OLLAMA

    set_method(provider, model, **kwargs)


def _update_model_settings(
    orchestrator: str | None,
    cypher: str | None,
) -> None:
    if orchestrator:
        _update_single_model_setting(ModelRole.ORCHESTRATOR, orchestrator)
    if cypher:
        _update_single_model_setting(ModelRole.CYPHER, cypher)


def _write_graph_json(ingestor: MemgraphIngestor, output_path: Path) -> dict:
    graph_data = ingestor.export_graph_to_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(graph_data, f, indent=2, ensure_ascii=False)

    return graph_data


def _connect_memgraph(batch_size: int) -> MemgraphIngestor:
    return MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=batch_size,
    )


def _export_graph_to_file(ingestor: MemgraphIngestor, output: str) -> bool:
    output_path = Path(output)

    try:
        graph_data = _write_graph_json(ingestor, output_path)
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


def _validate_provider_config(role: ModelRole, config: "ModelConfig") -> None:
    from .providers.base import get_provider

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
        raise ValueError(f"{role.value.title()} configuration error: {e}") from e


def _initialize_services_and_agent(
    repo_path: str, ingestor: QueryProtocol
) -> "Agent[None, str | DeferredToolRequests]":
    _validate_provider_config(
        ModelRole.ORCHESTRATOR, settings.active_orchestrator_config
    )
    _validate_provider_config(ModelRole.CYPHER, settings.active_cypher_config)

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
    project_root = _setup_common_initialization(repo_path)

    table = _create_configuration_table(repo_path)
    console.print(table)

    with _connect_memgraph(batch_size) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")
        console.print(
            Panel(
                "[bold yellow]Ask questions about your codebase graph. Type 'exit' or 'quit' to end.[/bold yellow]",
                border_style="yellow",
            )
        )

        rag_agent = _initialize_services_and_agent(repo_path, ingestor)
        await run_chat_loop(rag_agent, [], project_root)


async def main_optimize_async(
    language: str,
    target_repo_path: str,
    reference_document: str | None = None,
    orchestrator: str | None = None,
    cypher: str | None = None,
    batch_size: int | None = None,
) -> None:
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

    with _connect_memgraph(effective_batch_size) as ingestor:
        console.print("[bold green]Successfully connected to Memgraph.[/bold green]")

        rag_agent = _initialize_services_and_agent(target_repo_path, ingestor)
        await run_optimization_loop(
            rag_agent, [], project_root, language, reference_document
        )
