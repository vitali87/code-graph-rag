from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    ToolCallPart,
)

from codebase_rag.services.anthropic_token_counter import (
    _to_anthropic_payload,
    count_anthropic_context,
)


def _fake_post_returning(input_tokens: int) -> tuple[AsyncMock, MagicMock]:
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"input_tokens": input_tokens}
    fake_post = AsyncMock(return_value=fake_response)
    return fake_post, fake_response


@pytest.mark.asyncio
async def test_returns_zero_when_no_messages_and_no_system_prompt() -> None:
    with patch("httpx.AsyncClient") as mock_client:
        result = await count_anthropic_context(
            api_key="k", model_id="claude-opus-4-7", messages=[]
        )

    assert result == 0
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_injects_placeholder_when_only_system_prompt_present() -> None:
    fake_post, _ = _fake_post_returning(input_tokens=42_000)
    mock_client_instance = MagicMock()
    mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
    mock_client_instance.__aexit__ = AsyncMock(return_value=None)
    mock_client_instance.post = fake_post

    messages = [
        ModelRequest(parts=[SystemPromptPart(content="GIANT SYSTEM PROMPT BODY")])
    ]

    with patch("httpx.AsyncClient", return_value=mock_client_instance):
        result = await count_anthropic_context(
            api_key="k", model_id="claude-opus-4-7", messages=messages
        )

    assert result == 42_000
    payload: dict[str, Any] = fake_post.call_args.kwargs["json"]
    assert payload["system"] == "GIANT SYSTEM PROMPT BODY"
    assert payload["messages"]
    assert payload["messages"][0]["role"] == "user"
    placeholder_text = payload["messages"][0]["content"][0]["text"]
    assert placeholder_text.strip(), "placeholder must be non-whitespace"


def test_retry_prompt_with_tool_name_becomes_tool_result_error_block() -> None:
    tool_call_id = "toolu_test123"
    messages = [
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="semantic_search",
                    args={"query": "x"},
                    tool_call_id=tool_call_id,
                )
            ]
        ),
        ModelRequest(
            parts=[
                RetryPromptPart(
                    content="bad args",
                    tool_name="semantic_search",
                    tool_call_id=tool_call_id,
                )
            ]
        ),
    ]

    _, anthropic_messages = _to_anthropic_payload(messages)

    assert len(anthropic_messages) == 2
    assistant = anthropic_messages[0]
    user = anthropic_messages[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0]["type"] == "tool_use"
    assert assistant["content"][0]["id"] == tool_call_id
    assert user["role"] == "user"
    assert user["content"][0]["type"] == "tool_result"
    assert user["content"][0]["tool_use_id"] == tool_call_id
    assert user["content"][0]["is_error"] is True


def test_retry_prompt_without_tool_name_becomes_text_block() -> None:
    messages = [
        ModelRequest(parts=[RetryPromptPart(content="please retry")]),
    ]

    _, anthropic_messages = _to_anthropic_payload(messages)

    assert len(anthropic_messages) == 1
    assert anthropic_messages[0]["role"] == "user"
    assert anthropic_messages[0]["content"][0]["type"] == "text"
    assert "please retry" in anthropic_messages[0]["content"][0]["text"]
