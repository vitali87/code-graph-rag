from __future__ import annotations

import json

import httpx2
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.tools import ToolDefinition

from codebase_rag import constants as cs
from codebase_rag.services.anthropic_token_counter import count_anthropic_context

_READ_FILE = ToolDefinition(
    name="read_file",
    description="Read a file from the repository.",
    parameters_json_schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
)


def _counting_model(
    bodies: list[dict[str, object]], input_tokens: int = 42_000
) -> AnthropicModel:
    """An `AnthropicModel` whose count requests are recorded, not sent."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json={"input_tokens": input_tokens})

    provider = AnthropicProvider(
        api_key="k",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicModel("claude-opus-5-5", provider=provider)


@pytest.mark.asyncio
async def test_returns_zero_without_a_request_when_there_are_no_messages() -> None:
    bodies: list[dict[str, object]] = []

    assert await count_anthropic_context(_counting_model(bodies), []) == 0
    assert bodies == []


@pytest.mark.asyncio
async def test_a_bare_system_prompt_is_counted_with_a_placeholder_turn() -> None:
    # The endpoint rejects a request without messages, and the primed
    # counter holds only the system prompt before the first turn.
    bodies: list[dict[str, object]] = []
    messages = [
        ModelRequest(parts=[SystemPromptPart(content="GIANT SYSTEM PROMPT BODY")])
    ]

    count = await count_anthropic_context(_counting_model(bodies), messages)

    assert count == 42_000
    (body,) = bodies
    assert body["system"] == "GIANT SYSTEM PROMPT BODY"
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": cs.ANTHROPIC_COUNT_PLACEHOLDER_PROMPT}
            ],
        }
    ]


@pytest.mark.asyncio
async def test_the_tool_definitions_are_counted() -> None:
    # Every turn sends the agent's tool schemas, so a count without them
    # came up short by all of them.
    bodies: list[dict[str, object]] = []
    messages = [ModelRequest(parts=[UserPromptPart(content="hello")])]

    await count_anthropic_context(_counting_model(bodies), messages, [_READ_FILE])

    (body,) = bodies
    assert body["tools"] == [
        {
            "name": "read_file",
            "description": "Read a file from the repository.",
            "input_schema": _READ_FILE.parameters_json_schema,
        }
    ]


@pytest.mark.asyncio
async def test_a_tool_round_trip_reaches_the_count_as_the_model_sends_it() -> None:
    bodies: list[dict[str, object]] = []
    messages = [
        ModelRequest(parts=[UserPromptPart(content="read it")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": "a.py"},
                    tool_call_id="toolu_1",
                )
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read_file", content="x = 1", tool_call_id="toolu_1"
                )
            ]
        ),
    ]

    await count_anthropic_context(_counting_model(bodies), messages, [_READ_FILE])

    (body,) = bodies
    turns = body["messages"]
    assert isinstance(turns, list)
    assert [turn["role"] for turn in turns] == ["user", "assistant", "user"]
    assert turns[1]["content"][0]["type"] == "tool_use"
    assert turns[2]["content"][0]["type"] == "tool_result"
    assert turns[2]["content"][0]["tool_use_id"] == "toolu_1"
