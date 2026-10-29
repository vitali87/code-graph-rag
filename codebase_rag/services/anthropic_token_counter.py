"""The exact context size of an Anthropic conversation, as the model counts it.

Counted through the same `AnthropicModel` the agent runs on, so the request
is the one the next turn would send: its messages mapped by pydantic-ai, its
system prompt, and its tool definitions, which cost tokens on every turn.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from .. import constants as cs


class TokenCountError(Exception):
    pass


class TokenCountAuthError(TokenCountError):
    """The provider rejected the credential (401/403).

    Distinguished from the base error because the two need opposite
    responses. A rejected key will NEVER succeed, so retrying is pointless
    and the user has to change something; a 429 or 5xx is transient and the
    next refresh probably works.

    Collapsing them left an invalid API key reported as an internal
    token-count failure, with the provider's raw JSON as the only clue
    (issue #1493).

    Subclasses `TokenCountError` deliberately: an existing
    `except TokenCountError` caller keeps working rather than having this
    escape as an unhandled exception in a background task.
    """


def _countable(messages: list[ModelMessage]) -> list[ModelMessage]:
    # A history of system parts alone (the primed counter before the first
    # turn) maps to no messages, which the endpoint rejects.
    if any(
        not isinstance(part, SystemPromptPart)
        for message in messages
        for part in message.parts
    ):
        return messages
    return [
        *messages,
        ModelRequest(parts=[UserPromptPart(cs.ANTHROPIC_COUNT_PLACEHOLDER_PROMPT)]),
    ]


async def count_anthropic_context(
    model: AnthropicModel,
    messages: list[ModelMessage],
    tools: Sequence[ToolDefinition] = (),
) -> int:
    """The input tokens `messages` and `tools` cost `model` on the next turn."""
    if not messages:
        return 0
    try:
        usage = await model.count_tokens(
            _countable(messages),
            ModelSettings(timeout=cs.ANTHROPIC_COUNT_TIMEOUT_S),
            ModelRequestParameters(function_tools=list(tools)),
        )
    except ModelHTTPError as e:
        if e.status_code in cs.HTTP_AUTH_FAILURE_STATUSES:
            # The actionable part leads. The status is kept for diagnosis,
            # but a reader should not have to parse a provider JSON blob to
            # learn their key is wrong.
            raise TokenCountAuthError(
                f"the Anthropic API key was rejected ({e.status_code}); "
                "check the key configured for this provider"
            ) from e
        raise TokenCountError(f"{e.status_code}: {e.body}") from e
    except ModelAPIError as e:
        raise TokenCountError(str(e)) from e
    return usage.input_tokens
