"""Regression tests for issues #1199 and #1201: the orchestrator system prompt
must only reference tool names that are actually registered on the agent, and
must build its strategy sections conditionally on tool availability."""

from loguru import logger
from pydantic_ai import Tool

from codebase_rag.prompts import (
    build_rag_orchestrator_prompt,
    extract_registered_tools,
    extract_tool_names,
)
from codebase_rag.tools.tool_descriptions import AgenticToolName

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


def _tools_without(excluded: AgenticToolName) -> list[Tool]:
    return [tool for tool in _all_registered_tools() if tool.name != str(excluded)]


def test_extract_tool_names_returns_registered_names() -> None:
    tools = _all_registered_tools()
    registered = {tool.name for tool in tools}

    names = extract_tool_names(tools)

    for field, value in names._asdict().items():
        assert value in registered, f"{field} resolved to unregistered '{value}'"


def test_prompt_references_registered_names_not_stale_ones() -> None:
    tools = _all_registered_tools()
    registered = {tool.name for tool in tools}

    prompt = build_rag_orchestrator_prompt(tools)

    for stale in STALE_TOOL_NAMES:
        assert stale not in prompt
    for value in extract_tool_names(tools):
        assert value in registered
        assert f"`{value}`" in prompt


def test_extract_tool_names_tolerates_missing_tool() -> None:
    tools = _tools_without(AgenticToolName.SEMANTIC_SEARCH)

    names = extract_tool_names(tools)

    assert names.semantic_search == str(AgenticToolName.SEMANTIC_SEARCH)


def test_extract_registered_tools_reflects_registration() -> None:
    registered = extract_registered_tools(
        _tools_without(AgenticToolName.SEMANTIC_SEARCH)
    )

    assert AgenticToolName.SEMANTIC_SEARCH not in registered
    assert AgenticToolName.QUERY_GRAPH in registered


def test_extract_registered_tools_full_toolset() -> None:
    registered = extract_registered_tools(_all_registered_tools())

    assert registered == frozenset(AgenticToolName)


def test_prompt_omits_semantic_search_when_unregistered() -> None:
    tools = _tools_without(AgenticToolName.SEMANTIC_SEARCH)

    prompt = build_rag_orchestrator_prompt(tools)

    assert f"`{AgenticToolName.SEMANTIC_SEARCH}`" not in prompt
    assert "semantic" not in prompt.lower()
    assert "GRAPH FIRST" in prompt
    assert f"`{AgenticToolName.QUERY_GRAPH}`" in prompt
    assert f"`{AgenticToolName.READ_FILE}`" in prompt


def test_prompt_keeps_semantic_strategy_with_full_toolset() -> None:
    prompt = build_rag_orchestrator_prompt(_all_registered_tools())

    assert "SEMANTIC FIRST" in prompt
    assert "WHEN TO USE SEMANTIC SEARCH FIRST" in prompt
    assert "HYBRID APPROACH (RECOMMENDED)" in prompt
    assert f"`{AgenticToolName.SEMANTIC_SEARCH}`" in prompt


def test_missing_semantic_search_does_not_warn_but_missing_required_does() -> None:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="WARNING")
    try:
        extract_tool_names(_tools_without(AgenticToolName.SEMANTIC_SEARCH))
        assert not any(str(AgenticToolName.SEMANTIC_SEARCH) in m for m in messages)

        extract_tool_names(_tools_without(AgenticToolName.QUERY_GRAPH))
        assert any(str(AgenticToolName.QUERY_GRAPH) in m for m in messages)
    finally:
        logger.remove(sink_id)
