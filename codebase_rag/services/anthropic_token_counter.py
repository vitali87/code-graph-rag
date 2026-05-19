from __future__ import annotations

import base64
from typing import Any

import httpx
from pydantic_ai import BinaryContent
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


def _binary_block(item: BinaryContent) -> dict[str, Any]:
    media = item.media_type or cs.MIME_TYPE_FALLBACK
    block_type = "image" if media.startswith("image/") else "document"
    return {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media,
            "data": base64.b64encode(item.data).decode(),
        },
    }


def _user_part_to_blocks(part: UserPromptPart) -> list[dict[str, Any]]:
    content = part.content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    for item in content:
        if isinstance(item, str):
            blocks.append({"type": "text", "text": item})
        elif isinstance(item, BinaryContent):
            blocks.append(_binary_block(item))
    return blocks


def _tool_return_content(value: object) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, str):
                out.append({"type": "text", "text": item})
            elif isinstance(item, BinaryContent):
                out.append(_binary_block(item))
        if out:
            return out
    return str(value)


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
                    user_content.extend(_user_part_to_blocks(part))
                elif isinstance(part, ToolReturnPart):
                    user_content.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": part.tool_call_id,
                            "content": _tool_return_content(part.content),
                        }
                    )
            if user_content:
                out.append({"role": "user", "content": user_content})
        elif isinstance(m, ModelResponse):
            assistant_content: list[dict[str, Any]] = []
            for part in m.parts:
                if isinstance(part, TextPart):
                    if part.content:
                        assistant_content.append({"type": "text", "text": part.content})
                elif isinstance(part, ToolCallPart):
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": part.tool_call_id,
                            "name": part.tool_name,
                            "input": part.args_as_dict() or {},
                        }
                    )
            if assistant_content:
                out.append({"role": "assistant", "content": assistant_content})
    return "\n".join(system_parts), out


class TokenCountError(Exception):
    pass


async def count_anthropic_context(
    api_key: str,
    model_id: str,
    messages: list[ModelMessage],
) -> int:
    system_prompt, anthropic_messages = _to_anthropic_payload(messages)
    if not anthropic_messages:
        if not system_prompt:
            return 0
        anthropic_messages = [
            {"role": "user", "content": [{"type": "text", "text": "."}]}
        ]
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
        if resp.status_code >= 400:
            raise TokenCountError(f"{resp.status_code}: {resp.text}")
        return int(resp.json().get("input_tokens", 0))
