from __future__ import annotations

from typing import Any

import httpx
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

from .. import constants as cs


def _to_anthropic_payload(
    messages: list[ModelMessage],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            user_content: list[dict[str, Any]] = []
            for part in m.parts:
                if isinstance(part, SystemPromptPart):
                    system_parts.append(part.content)
                elif isinstance(part, UserPromptPart):
                    user_content.append({"type": "text", "text": str(part.content)})
                elif isinstance(part, ToolReturnPart):
                    user_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_call_id,
                            "content": str(part.content),
                        }
                    )
            if user_content:
                out.append({"role": "user", "content": user_content})
        elif isinstance(m, ModelResponse):
            assistant_content: list[dict[str, Any]] = []
            for part in m.parts:
                if isinstance(part, TextPart):
                    assistant_content.append({"type": "text", "text": part.content})
                elif isinstance(part, ToolCallPart):
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": part.tool_call_id,
                            "name": part.tool_name,
                            "input": part.args_as_dict(),
                        }
                    )
            if assistant_content:
                out.append({"role": "assistant", "content": assistant_content})
    return "\n".join(system_parts), out


async def count_anthropic_context(
    api_key: str,
    model_id: str,
    messages: list[ModelMessage],
) -> int:
    system_prompt, anthropic_messages = _to_anthropic_payload(messages)
    if not anthropic_messages:
        return 0
    payload: dict[str, Any] = {
        "model": model_id,
        "messages": anthropic_messages,
    }
    if system_prompt:
        payload["system"] = system_prompt
    headers = {
        cs.ANTHROPIC_HEADER_API_KEY: api_key,
        cs.ANTHROPIC_HEADER_VERSION: cs.ANTHROPIC_API_VERSION,
        cs.HEADER_CONTENT_TYPE: cs.CONTENT_TYPE_JSON,
    }
    async with httpx.AsyncClient(timeout=cs.ANTHROPIC_COUNT_TIMEOUT_S) as client:
        resp = await client.post(
            cs.ANTHROPIC_COUNT_TOKENS_URL, json=payload, headers=headers
        )
        resp.raise_for_status()
        return int(resp.json().get("input_tokens", 0))
