from __future__ import annotations

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
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from .config import CHAT_LOOP_CONFIG, OPTIMIZATION_LOOP_CONFIG, ORANGE_STYLE, settings
from .constants import (
    ARG_COMMAND,
    ARG_CONTENT,
    ARG_FILE_PATH,
    ARG_REPLACEMENT_CODE,
    ARG_TARGET_CODE,
    CONFIRM_DISABLED,
    CONFIRM_ENABLED,
    DEFAULT_TABLE_TITLE,
    DIFF_LABEL_AFTER,
    DIFF_LABEL_BEFORE,
    ENCODING_UTF8,
    ERR_CONFIG,
    ERR_EXPORT_ERROR,
    ERR_IMAGE_COPY_FAILED,
    ERR_IMAGE_NOT_FOUND,
    ERR_PATH_NOT_IN_QUESTION,
    ERR_UNEXPECTED,
    EXIT_COMMANDS,
    FIELD_API_KEY,
    FIELD_ENDPOINT,
    HORIZONTAL_SEPARATOR,
    IMAGE_EXTENSIONS,
    JSON_INDENT,
    KEY_METADATA,
    KEY_TOTAL_NODES,
    KEY_TOTAL_RELATIONSHIPS,
    LOG_FORMAT,
    LOG_IMAGE_COPIED,
    MSG_CHAT_INSTRUCTIONS,
    MSG_CONNECTED_MEMGRAPH,
    MSG_THINKING_CANCELLED,
    MSG_TIMEOUT_FORMAT,
    MULTILINE_INPUT_HINT,
    OPTIMIZATION_TABLE_TITLE,
    PROMPT_ASK_QUESTION,
    PROMPT_YOUR_RESPONSE,
    SESSION_CONTEXT_END,
    SESSION_CONTEXT_START,
    SESSION_LOG_HEADER,
    SESSION_LOG_PREFIX,
    SESSION_PREFIX_ASSISTANT,
    SESSION_PREFIX_USER,
    TABLE_COL_CONFIGURATION,
    TABLE_COL_VALUE,
    TABLE_ROW_CYPHER_MODEL,
    TABLE_ROW_EDIT_CONFIRMATION,
    TABLE_ROW_OLLAMA_CYPHER,
    TABLE_ROW_OLLAMA_ENDPOINT,
    TABLE_ROW_OLLAMA_ORCHESTRATOR,
    TABLE_ROW_ORCHESTRATOR_MODEL,
    TABLE_ROW_TARGET_LANGUAGE,
    TABLE_ROW_TARGET_REPOSITORY,
    TMP_DIR,
    UI_DIFF_FILE_HEADER,
    UI_ERR_EXPORT_FAILED,
    UI_ERR_UNEXPECTED,
    UI_FEEDBACK_PROMPT,
    UI_GRAPH_EXPORT_STATS,
    UI_GRAPH_EXPORT_SUCCESS,
    UI_NEW_FILE_HEADER,
    UI_OPTIMIZATION_INIT,
    UI_OPTIMIZATION_PANEL,
    UI_OPTIMIZATION_START,
    UI_SHELL_COMMAND_HEADER,
    UI_TOOL_APPROVAL,
    Color,
    DiffMarker,
    KeyBinding,
    ModelRole,
    Provider,
    StyleModifier,
    ToolName,
)
from .models import AgentLoopConfig, AppContext
from .prompts import OPTIMIZATION_PROMPT, OPTIMIZATION_PROMPT_WITH_REFERENCE
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
from .types_defs import (
    CancelledResult,
    CreateFileArgs,
    GraphData,
    ReplaceCodeArgs,
    ShellCommandArgs,
    ToolArgs,
)

if TYPE_CHECKING:
    from prompt_toolkit.key_binding import KeyPressEvent
    from pydantic_ai import Agent
    from pydantic_ai.messages import ModelMessage

    from .config import ModelConfig


def style(text: str, color: Color, modifier: StyleModifier = StyleModifier.BOLD) -> str:
    if modifier == StyleModifier.NONE:
        return f"[{color}]{text}[/{color}]"
    return f"[{modifier} {color}]{text}[/{modifier} {color}]"


def dim(text: str) -> str:
    return f"[{StyleModifier.DIM}]{text}[/{StyleModifier.DIM}]"


app_context = AppContext()


def init_session_log(project_root: Path) -> Path:
    log_dir = project_root / TMP_DIR
    log_dir.mkdir(exist_ok=True)
    app_context.session.log_file = (
        log_dir / f"{SESSION_LOG_PREFIX}{uuid.uuid4().hex[:8]}.log"
    )
    with open(app_context.session.log_file, "w") as f:
        f.write(SESSION_LOG_HEADER)
    return app_context.session.log_file


def log_session_event(event: str) -> None:
    if app_context.session.log_file:
        with open(app_context.session.log_file, "a") as f:
            f.write(f"{event}\n")


def get_session_context() -> str:
    if app_context.session.log_file and app_context.session.log_file.exists():
        content = app_context.session.log_file.read_text()
        return f"{SESSION_CONTEXT_START}{content}{SESSION_CONTEXT_END}"
    return ""


def _print_unified_diff(target: str, replacement: str, path: str) -> None:
    separator = dim(HORIZONTAL_SEPARATOR)
    app_context.console.print(f"\n{UI_DIFF_FILE_HEADER.format(path=path)}")
    app_context.console.print(separator)

    diff = difflib.unified_diff(
        target.splitlines(keepends=True),
        replacement.splitlines(keepends=True),
        fromfile=DIFF_LABEL_BEFORE,
        tofile=DIFF_LABEL_AFTER,
        lineterm="",
    )

    for line in diff:
        line = line.rstrip("\n")
        match line[:1]:
            case DiffMarker.ADD | DiffMarker.DEL if line.startswith(
                DiffMarker.HEADER_ADD
            ) or line.startswith(DiffMarker.HEADER_DEL):
                app_context.console.print(dim(line))
            case DiffMarker.HUNK:
                app_context.console.print(style(line, Color.CYAN, StyleModifier.NONE))
            case DiffMarker.ADD:
                app_context.console.print(style(line, Color.GREEN, StyleModifier.NONE))
            case DiffMarker.DEL:
                app_context.console.print(style(line, Color.RED, StyleModifier.NONE))
            case _:
                app_context.console.print(line)

    app_context.console.print(separator)


def _print_new_file_content(path: str, content: str) -> None:
    separator = dim(HORIZONTAL_SEPARATOR)
    app_context.console.print(f"\n{UI_NEW_FILE_HEADER.format(path=path)}")
    app_context.console.print(separator)

    for line in content.splitlines():
        app_context.console.print(
            style(f"{DiffMarker.ADD} {line}", Color.GREEN, StyleModifier.NONE)
        )

    app_context.console.print(separator)


def _to_tool_args(tool_name: str, raw_args: dict[str, str]) -> ToolArgs:
    match tool_name:
        case ToolName.REPLACE_CODE:
            return ReplaceCodeArgs(
                file_path=raw_args.get(ARG_FILE_PATH, ""),
                target_code=raw_args.get(ARG_TARGET_CODE, ""),
                replacement_code=raw_args.get(ARG_REPLACEMENT_CODE, ""),
            )
        case ToolName.CREATE_FILE:
            return CreateFileArgs(
                file_path=raw_args.get(ARG_FILE_PATH, ""),
                content=raw_args.get(ARG_CONTENT, ""),
            )
        case ToolName.SHELL_COMMAND:
            return ShellCommandArgs(command=raw_args.get(ARG_COMMAND, ""))
        case _:
            return ShellCommandArgs()


def _display_tool_call_diff(
    tool_name: str, tool_args: ToolArgs, file_path: str | None = None
) -> None:
    match tool_name:
        case ToolName.REPLACE_CODE:
            target = str(tool_args.get(ARG_TARGET_CODE, ""))
            replacement = str(tool_args.get(ARG_REPLACEMENT_CODE, ""))
            path = str(tool_args.get(ARG_FILE_PATH, file_path or "file"))
            _print_unified_diff(target, replacement, path)

        case ToolName.CREATE_FILE:
            path = str(tool_args.get(ARG_FILE_PATH, ""))
            content = str(tool_args.get(ARG_CONTENT, ""))
            _print_new_file_content(path, content)

        case ToolName.SHELL_COMMAND:
            command = tool_args.get(ARG_COMMAND, "")
            app_context.console.print(f"\n{UI_SHELL_COMMAND_HEADER}")
            app_context.console.print(
                style(f"$ {command}", Color.YELLOW, StyleModifier.NONE)
            )

        case _:
            app_context.console.print(
                f"    Arguments: {json.dumps(tool_args, indent=JSON_INDENT)}"
            )


def _process_tool_approvals(
    requests: DeferredToolRequests, approval_prompt: str, denial_default: str
) -> DeferredToolResults:
    deferred_results = DeferredToolResults()

    for call in requests.approvals:
        tool_args = _to_tool_args(call.tool_name, call.args_as_dict())
        app_context.console.print(
            f"\n{UI_TOOL_APPROVAL.format(tool_name=call.tool_name)}"
        )
        _display_tool_call_diff(call.tool_name, tool_args)

        if app_context.session.confirm_edits:
            if Confirm.ask(style(approval_prompt, Color.CYAN)):
                deferred_results.approvals[call.tool_call_id] = True
            else:
                feedback = Prompt.ask(
                    UI_FEEDBACK_PROMPT,
                    default="",
                )
                denial_msg = feedback.strip() or denial_default
                deferred_results.approvals[call.tool_call_id] = ToolDenied(denial_msg)
        else:
            deferred_results.approvals[call.tool_call_id] = True

    return deferred_results


def _setup_common_initialization(repo_path: str) -> Path:
    logger.remove()
    logger.add(sys.stdout, format=LOG_FORMAT)

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
    title: str = DEFAULT_TABLE_TITLE,
    language: str | None = None,
) -> Table:
    table = Table(title=style(title, Color.GREEN))
    table.add_column(TABLE_COL_CONFIGURATION, style=Color.CYAN)
    table.add_column(TABLE_COL_VALUE, style=Color.MAGENTA)

    if language:
        table.add_row(TABLE_ROW_TARGET_LANGUAGE, language)

    orchestrator_config = settings.active_orchestrator_config
    table.add_row(
        TABLE_ROW_ORCHESTRATOR_MODEL,
        f"{orchestrator_config.model_id} ({orchestrator_config.provider})",
    )

    cypher_config = settings.active_cypher_config
    table.add_row(
        TABLE_ROW_CYPHER_MODEL,
        f"{cypher_config.model_id} ({cypher_config.provider})",
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
        table.add_row(TABLE_ROW_OLLAMA_ENDPOINT, orch_endpoint)
    else:
        if orch_endpoint:
            table.add_row(TABLE_ROW_OLLAMA_ORCHESTRATOR, orch_endpoint)
        if cypher_endpoint:
            table.add_row(TABLE_ROW_OLLAMA_CYPHER, cypher_endpoint)

    confirmation_status = (
        CONFIRM_ENABLED if app_context.session.confirm_edits else CONFIRM_DISABLED
    )
    table.add_row(TABLE_ROW_EDIT_CONFIRMATION, confirmation_status)
    table.add_row(TABLE_ROW_TARGET_REPOSITORY, repo_path)

    return table


async def run_optimization_loop(
    rag_agent: Agent[None, str | DeferredToolRequests],
    message_history: list[ModelMessage],
    project_root: Path,
    language: str,
    reference_document: str | None = None,
) -> None:
    app_context.console.print(UI_OPTIMIZATION_START.format(language=language))
    document_info = (
        f" using the reference document: {reference_document}"
        if reference_document
        else ""
    )
    app_context.console.print(
        Panel(
            UI_OPTIMIZATION_PANEL.format(document_info=document_info),
            border_style=Color.YELLOW,
        )
    )

    initial_question = (
        OPTIMIZATION_PROMPT_WITH_REFERENCE.format(
            language=language, reference_document=reference_document
        )
        if reference_document
        else OPTIMIZATION_PROMPT.format(language=language)
    )

    await _run_interactive_loop(
        rag_agent,
        message_history,
        project_root,
        OPTIMIZATION_LOOP_CONFIG,
        style(PROMPT_YOUR_RESPONSE, Color.CYAN),
        initial_question,
    )


async def run_with_cancellation[T](
    coro: Coroutine[None, None, T], timeout: float | None = None
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
        app_context.console.print(
            f"\n{style(MSG_TIMEOUT_FORMAT.format(timeout=timeout), Color.YELLOW)}"
        )
        return CancelledResult(cancelled=True)
    except (asyncio.CancelledError, KeyboardInterrupt):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        app_context.console.print(f"\n{style(MSG_THINKING_CANCELLED, Color.YELLOW)}")
        return CancelledResult(cancelled=True)


async def _run_agent_response_loop(
    rag_agent: Agent[None, str | DeferredToolRequests],
    message_history: list[ModelMessage],
    question_with_context: str,
    config: AgentLoopConfig,
) -> None:
    deferred_results: DeferredToolResults | None = None

    while True:
        with app_context.console.status(config.status_message):
            response = await run_with_cancellation(
                rag_agent.run(
                    question_with_context,
                    message_history=message_history,
                    deferred_tool_results=deferred_results,
                ),
            )

        if isinstance(response, CancelledResult):
            log_session_event(config.cancelled_log)
            app_context.session.cancelled = True
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
        app_context.console.print(
            Panel(
                markdown_response,
                title=config.panel_title,
                border_style=Color.GREEN,
            )
        )

        log_session_event(f"{SESSION_PREFIX_ASSISTANT}{output_text}")
        message_history.extend(response.new_messages())
        break


def _find_image_paths(question: str) -> list[str]:
    try:
        tokens = shlex.split(question)
    except ValueError:
        tokens = question.split()
    return [
        token
        for token in tokens
        if token.startswith("/") and token.lower().endswith(IMAGE_EXTENSIONS)
    ]


def _get_path_variants(path_str: str) -> tuple[str, ...]:
    return (
        path_str.replace(" ", r"\ "),
        f"'{path_str}'",
        f'"{path_str}"',
        path_str,
    )


def _replace_path_in_question(question: str, old_path: str, new_path: str) -> str:
    for variant in _get_path_variants(old_path):
        if variant in question:
            return question.replace(variant, new_path)
    logger.warning(ERR_PATH_NOT_IN_QUESTION.format(path=old_path))
    return question


def _handle_chat_images(question: str, project_root: Path) -> str:
    image_files = _find_image_paths(question)
    if not image_files:
        return question

    tmp_dir = project_root / TMP_DIR
    tmp_dir.mkdir(exist_ok=True)
    updated_question = question

    for original_path_str in image_files:
        original_path = Path(original_path_str)

        if not original_path.exists() or not original_path.is_file():
            logger.warning(ERR_IMAGE_NOT_FOUND.format(path=original_path_str))
            continue

        try:
            new_path = tmp_dir / f"{uuid.uuid4()}-{original_path.name}"
            shutil.copy(original_path, new_path)
            new_relative = str(new_path.relative_to(project_root))
            updated_question = _replace_path_in_question(
                updated_question, original_path_str, new_relative
            )
            logger.info(LOG_IMAGE_COPIED.format(path=new_relative))
        except Exception as e:
            logger.error(ERR_IMAGE_COPY_FAILED.format(error=e))

    return updated_question


def get_multiline_input(prompt_text: str = PROMPT_ASK_QUESTION) -> str:
    bindings = KeyBindings()

    @bindings.add(KeyBinding.CTRL_J)
    def submit(event: KeyPressEvent) -> None:
        event.app.exit(result=event.app.current_buffer.text)

    @bindings.add(KeyBinding.ENTER)
    def new_line(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add(KeyBinding.CTRL_C)
    def keyboard_interrupt(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    clean_prompt = Text.from_markup(prompt_text).plain

    print_formatted_text(
        HTML(
            f"<ansigreen><b>{clean_prompt}</b></ansigreen> <ansiyellow>{MULTILINE_INPUT_HINT}</ansiyellow>: "
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


async def _run_interactive_loop(
    rag_agent: Agent[None, str | DeferredToolRequests],
    message_history: list[ModelMessage],
    project_root: Path,
    config: AgentLoopConfig,
    input_prompt: str,
    initial_question: str | None = None,
) -> None:
    init_session_log(project_root)
    question = initial_question or ""

    while True:
        try:
            if not initial_question or question != initial_question:
                question = await asyncio.to_thread(get_multiline_input, input_prompt)

            if question.lower() in EXIT_COMMANDS:
                break
            if not question.strip():
                initial_question = None
                continue

            log_session_event(f"{SESSION_PREFIX_USER}{question}")

            if app_context.session.cancelled:
                question_with_context = question + get_session_context()
                app_context.session.reset_cancelled()
            else:
                question_with_context = question

            question_with_context = _handle_chat_images(
                question_with_context, project_root
            )

            await _run_agent_response_loop(
                rag_agent, message_history, question_with_context, config
            )

            initial_question = None

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(ERR_UNEXPECTED.format(error=e), exc_info=True)
            app_context.console.print(UI_ERR_UNEXPECTED.format(error=e))


async def run_chat_loop(
    rag_agent: Agent[None, str | DeferredToolRequests],
    message_history: list[ModelMessage],
    project_root: Path,
) -> None:
    await _run_interactive_loop(
        rag_agent,
        message_history,
        project_root,
        CHAT_LOOP_CONFIG,
        style(PROMPT_ASK_QUESTION, Color.CYAN),
    )


def _update_single_model_setting(role: ModelRole, model_string: str) -> None:
    provider, model = settings.parse_model_string(model_string)

    match role:
        case ModelRole.ORCHESTRATOR:
            current_config = settings.active_orchestrator_config
            set_method = settings.set_orchestrator
        case ModelRole.CYPHER:
            current_config = settings.active_cypher_config
            set_method = settings.set_cypher

    kwargs = current_config.to_update_kwargs()

    if provider == Provider.OLLAMA and not kwargs[FIELD_ENDPOINT]:
        kwargs[FIELD_ENDPOINT] = str(settings.LOCAL_MODEL_ENDPOINT)
        kwargs[FIELD_API_KEY] = Provider.OLLAMA

    set_method(provider, model, **kwargs)


def _update_model_settings(
    orchestrator: str | None,
    cypher: str | None,
) -> None:
    if orchestrator:
        _update_single_model_setting(ModelRole.ORCHESTRATOR, orchestrator)
    if cypher:
        _update_single_model_setting(ModelRole.CYPHER, cypher)


def _write_graph_json(ingestor: MemgraphIngestor, output_path: Path) -> GraphData:
    graph_data: GraphData = ingestor.export_graph_to_dict()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding=ENCODING_UTF8) as f:
        json.dump(graph_data, f, indent=JSON_INDENT, ensure_ascii=False)

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
        metadata = graph_data[KEY_METADATA]
        app_context.console.print(
            UI_GRAPH_EXPORT_SUCCESS.format(path=output_path.absolute())
        )
        app_context.console.print(
            UI_GRAPH_EXPORT_STATS.format(
                nodes=metadata[KEY_TOTAL_NODES],
                relationships=metadata[KEY_TOTAL_RELATIONSHIPS],
            )
        )
        return True

    except Exception as e:
        app_context.console.print(UI_ERR_EXPORT_FAILED.format(error=e))
        logger.error(ERR_EXPORT_ERROR.format(error=e), exc_info=True)
        return False


def _validate_provider_config(role: ModelRole, config: ModelConfig) -> None:
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
        raise ValueError(ERR_CONFIG.format(role=role.value.title(), error=e)) from e


def _initialize_services_and_agent(
    repo_path: str, ingestor: QueryProtocol
) -> Agent[None, str | DeferredToolRequests]:
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

    query_tool = create_query_tool(ingestor, cypher_generator, app_context.console)
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
    app_context.console.print(table)

    with _connect_memgraph(batch_size) as ingestor:
        app_context.console.print(style(MSG_CONNECTED_MEMGRAPH, Color.GREEN))
        app_context.console.print(
            Panel(
                style(MSG_CHAT_INSTRUCTIONS, Color.YELLOW),
                border_style=Color.YELLOW,
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

    app_context.console.print(
        UI_OPTIMIZATION_INIT.format(language=language, path=project_root)
    )

    table = _create_configuration_table(
        str(project_root), OPTIMIZATION_TABLE_TITLE, language
    )
    app_context.console.print(table)

    effective_batch_size = settings.resolve_batch_size(batch_size)

    with _connect_memgraph(effective_batch_size) as ingestor:
        app_context.console.print(style(MSG_CONNECTED_MEMGRAPH, Color.GREEN))

        rag_agent = _initialize_services_and_agent(target_repo_path, ingestor)
        await run_optimization_loop(
            rag_agent, [], project_root, language, reference_document
        )
