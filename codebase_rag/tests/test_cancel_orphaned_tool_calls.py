from __future__ import annotations

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from codebase_rag import constants as cs
from codebase_rag.main import _cancel_orphaned_tool_calls


def test_noop_when_history_empty() -> None:
    history: list[ModelMessage] = []
    _cancel_orphaned_tool_calls(history)
    assert history == []


def test_noop_when_last_message_is_request() -> None:
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="hi")])]
    _cancel_orphaned_tool_calls(history)
    assert len(history) == 1


def test_noop_when_response_has_no_tool_calls() -> None:
    history: list[ModelMessage] = [
        ModelRequest(parts=[SystemPromptPart(content="sys")]),
        ModelResponse(parts=[TextPart(content="hello")]),
    ]
    _cancel_orphaned_tool_calls(history)
    assert len(history) == 2


def test_appends_synthetic_return_for_each_orphan_tool_call() -> None:
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="run stuff")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="shell_command",
                    args={"command": "ls"},
                    tool_call_id="call_1",
                ),
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": "/tmp/x"},
                    tool_call_id="call_2",
                ),
            ]
        ),
    ]

    _cancel_orphaned_tool_calls(history)

    assert len(history) == 3
    repaired = history[-1]
    assert isinstance(repaired, ModelRequest)
    returns = [p for p in repaired.parts if isinstance(p, ToolReturnPart)]
    assert len(returns) == 2
    assert {r.tool_call_id for r in returns} == {"call_1", "call_2"}
    for r in returns:
        assert r.content == cs.MSG_TOOL_CALL_CANCELLED


def test_ignores_non_tool_call_parts_in_response() -> None:
    history: list[ModelMessage] = [
        ModelResponse(
            parts=[
                TextPart(content="some text"),
                ToolCallPart(
                    tool_name="shell_command",
                    args={"command": "ls"},
                    tool_call_id="call_1",
                ),
            ]
        ),
    ]

    _cancel_orphaned_tool_calls(history)

    assert len(history) == 2
    repaired = history[-1]
    assert isinstance(repaired, ModelRequest)
    returns = [p for p in repaired.parts if isinstance(p, ToolReturnPart)]
    assert len(returns) == 1
    assert returns[0].tool_call_id == "call_1"
