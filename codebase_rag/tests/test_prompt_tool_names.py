"""Regression tests for issue #1199: the orchestrator system prompt must only
reference tool names that are actually registered on the agent."""

from collections.abc import Iterator
from contextlib import contextmanager

from loguru import logger
from pydantic_ai import Tool

from codebase_rag.prompts import build_rag_orchestrator_prompt, extract_tool_names
from codebase_rag.tools.tool_descriptions import AgenticToolName
from codebase_rag.types_defs import ToolNames

STALE_TOOL_NAMES = (
    "query_codebase_knowledge_graph",
    "read_file_content",
    "semantic_code_search",
    "create_new_file",
    "replace_code_surgically",
    "execute_shell_command",
)


def _noop(**kwargs: object) -> str:
    return ""


def _all_registered_tools() -> list[Tool]:
    return [
        Tool(function=_noop, name=str(name), description=str(name), takes_ctx=False)
        for name in AgenticToolName
    ]


def _tool_name_values(names: ToolNames) -> list[str]:
    """The resolved tool names only, excluding availability flags."""
    return [value for value in names._asdict().values() if isinstance(value, str)]


def test_extract_tool_names_returns_registered_names() -> None:
    tools = _all_registered_tools()
    registered = {tool.name for tool in tools}

    names = extract_tool_names(tools)

    for field, value in names._asdict().items():
        if not isinstance(value, str):
            continue
        assert value in registered, f"{field} resolved to unregistered '{value}'"


def test_prompt_references_registered_names_not_stale_ones() -> None:
    tools = _all_registered_tools()
    registered = {tool.name for tool in tools}

    prompt = build_rag_orchestrator_prompt(tools)

    for stale in STALE_TOOL_NAMES:
        assert stale not in prompt
    for value in _tool_name_values(extract_tool_names(tools)):
        assert value in registered
        assert f"`{value}`" in prompt


def test_extract_tool_names_tolerates_missing_tool() -> None:
    tools = [
        tool
        for tool in _all_registered_tools()
        if tool.name != str(AgenticToolName.SEMANTIC_SEARCH)
    ]

    names = extract_tool_names(tools)

    assert names.semantic_search == str(AgenticToolName.SEMANTIC_SEARCH)


def _tools_without_semantic_search() -> list[Tool]:
    return [
        tool
        for tool in _all_registered_tools()
        if tool.name != str(AgenticToolName.SEMANTIC_SEARCH)
    ]


def test_extract_tool_names_reports_availability() -> None:
    """Issue #1201: callers need to know which canonical tools are registered,
    not just what they are named."""
    full = extract_tool_names(_all_registered_tools())
    assert full.has_semantic_search is True

    partial = extract_tool_names(_tools_without_semantic_search())
    assert partial.has_semantic_search is False


def test_prompt_omits_semantic_search_when_unregistered() -> None:
    """Issue #1201: with no semantic_search tool registered, the prompt must not
    instruct the model to call it -- a call to an unregistered name is dropped
    silently and burns retries on empty turns."""
    prompt = build_rag_orchestrator_prompt(_tools_without_semantic_search())

    semantic = str(AgenticToolName.SEMANTIC_SEARCH)
    assert f"`{semantic}`" not in prompt
    assert "WHEN TO USE SEMANTIC SEARCH FIRST" not in prompt
    assert "HYBRID APPROACH" not in prompt
    assert "semantic search" not in prompt.lower()


def test_prompt_keeps_graph_guidance_without_semantic_search() -> None:
    """The graph-first guidance must survive as the default strategy rather than
    disappearing along with the semantic-search subsection."""
    tools = _tools_without_semantic_search()
    names = extract_tool_names(tools)

    prompt = build_rag_orchestrator_prompt(tools)

    assert f"`{names.query_graph}`" in prompt
    assert f"`{names.read_file}`" in prompt
    assert "Search Strategy" in prompt


def test_prompt_retains_semantic_section_when_registered() -> None:
    """Guards against the strategy section silently disappearing for everyone."""
    prompt = build_rag_orchestrator_prompt(_all_registered_tools())

    assert "WHEN TO USE SEMANTIC SEARCH FIRST" in prompt
    assert "HYBRID APPROACH" in prompt
    assert f"`{AgenticToolName.SEMANTIC_SEARCH}`" in prompt


@contextmanager
def _captured_warnings() -> Iterator[list[str]]:
    """Collect loguru WARNING messages; loguru does not feed pytest's caplog."""
    messages: list[str] = []
    sink_id = logger.add(
        lambda message: messages.append(message.record["message"]), level="WARNING"
    )
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def test_absent_semantic_search_does_not_warn() -> None:
    """A supported absence must stay quiet.

    `resolve_tool_name` warns that "the orchestrator prompt references it
    anyway", which was true when every tool name was interpolated
    unconditionally. Since #1201 the graph-first branch deliberately omits
    `semantic_search`, so the warning fires on exactly the configuration the
    prompt now handles correctly, and it trains operators to ignore a warning
    that is real for the other five tools (CodeRabbit, #1446).
    """
    semantic = str(AgenticToolName.SEMANTIC_SEARCH)

    with _captured_warnings() as messages:
        extract_tool_names(_tools_without_semantic_search())

    assert not [m for m in messages if semantic in m], (
        f"warned about a supported semantic-search absence: {messages}"
    )


def test_absent_other_tool_still_warns() -> None:
    """The warning must survive for absences that are genuinely unhandled.

    Only `semantic_search` has a conditional prompt branch. Suppressing the
    warning wholesale would silence the five tools whose absence really does
    leave a dangling reference in the prompt.
    """
    read_file = str(AgenticToolName.READ_FILE)
    tools = [tool for tool in _all_registered_tools() if tool.name != read_file]

    with _captured_warnings() as messages:
        extract_tool_names(tools)

    assert [m for m in messages if read_file in m], (
        f"unhandled absence of {read_file} was not reported: {messages}"
    )
