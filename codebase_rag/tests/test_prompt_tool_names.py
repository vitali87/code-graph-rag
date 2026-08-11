"""Regression tests for issue #1199: the orchestrator system prompt must only
reference tool names that are actually registered on the agent."""

from pydantic_ai import Tool

from codebase_rag.prompts import build_rag_orchestrator_prompt, extract_tool_names
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
    tools = [
        tool
        for tool in _all_registered_tools()
        if tool.name != str(AgenticToolName.SEMANTIC_SEARCH)
    ]

    names = extract_tool_names(tools)

    assert names.semantic_search == str(AgenticToolName.SEMANTIC_SEARCH)
