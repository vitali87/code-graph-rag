# Authentication failures must be distinguishable from transient ones (#1493).
#
# Reported as "Context token count failed: 401: {...invalid x-api-key...}".
# The count was not broken -- the MESSAGE was. Every HTTP status >= 400 raised
# the same `TokenCountError`, and the caller logged them all identically at
# debug level, so an invalid API key looked like an internal hiccup.
#
# The two cases need opposite responses from the user:
#
#   401/403  the key is wrong; it will NEVER succeed, and no retry helps
#   429/5xx  transient; the next refresh probably works
#
# Collapsing them leaves the user decoding a raw provider JSON blob to work
# out which one they have.
from __future__ import annotations

import httpx
import pytest

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from codebase_rag.services.anthropic_token_counter import (
    TokenCountAuthError,
    TokenCountError,
    count_anthropic_context,
)


def _transport(status: int, body: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body)

    return httpx.MockTransport(handler)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport
) -> None:
    """Route `httpx.AsyncClient` through a mock transport.

    Captures the REAL class first. A lambda that calls `httpx.AsyncClient`
    after patching that same name recurses infinitely -- and the resulting
    `RecursionError` reads as a code failure rather than a fixture one.
    """
    real = httpx.AsyncClient

    def build(**kwargs: object) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx, "AsyncClient", build)


def _messages() -> list[ModelMessage]:
    """A message list that actually reaches the HTTP call.

    `count_anthropic_context` returns 0 before making any request when the
    payload is empty, so an `[]` fixture never reaches the code under test --
    the unreachable-fixture shape. Every test below asserts on the response
    to a request, so the request has to happen.
    """
    return [ModelRequest(parts=[UserPromptPart(content="hello")])]


class TestErrorClassification:
    """Which exception a status code produces."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403])
    async def test_an_auth_status_raises_the_auth_error(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """401 and 403 are configuration errors, not failures to count.

        Both mean the credential is rejected. Neither is retryable, so the
        caller needs to tell the user rather than log at debug and move on.
        """
        transport = _transport(
            status, '{"type":"error","error":{"message":"invalid x-api-key"}}'
        )
        _patch_client(monkeypatch, transport)

        with pytest.raises(TokenCountAuthError):
            await count_anthropic_context("bad-key", "claude-x", _messages())

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_a_transient_status_raises_the_plain_error(
        self, monkeypatch: pytest.MonkeyPatch, status: int
    ) -> None:
        """Rate limits and server errors stay the ordinary failure.

        The control that keeps the fix from over-reaching: if every status
        became an auth error, the caller would nag about credentials during a
        rate limit, which is worse than the original defect.
        """
        transport = _transport(status, '{"type":"error","error":{"message":"oops"}}')
        _patch_client(monkeypatch, transport)

        with pytest.raises(TokenCountError) as caught:
            await count_anthropic_context("good-key", "claude-x", _messages())

        assert not isinstance(caught.value, TokenCountAuthError), (
            f"status {status} was classified as an authentication failure; "
            "it is transient and a retry may succeed"
        )

    @pytest.mark.asyncio
    async def test_the_auth_error_is_a_token_count_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subclassing keeps existing `except TokenCountError` callers working.

        A separate exception hierarchy would silently escape any caller that
        already handles the base type -- turning a logged failure into a
        crash in a background task.
        """
        transport = _transport(401, "denied")
        _patch_client(monkeypatch, transport)

        with pytest.raises(TokenCountError):
            await count_anthropic_context("bad-key", "claude-x", _messages())


class TestMessage:
    """What the user is actually told."""

    @pytest.mark.asyncio
    async def test_the_auth_message_names_the_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message must say the key is invalid, not echo provider JSON.

        The report on #1493 shows the raw blob reaching the user:

            Context token count failed: 401: {"type":"error","error":...}

        A reader has to parse that to learn their key is wrong. The status
        code is retained for diagnosis, but the actionable part leads.
        """
        transport = _transport(
            401, '{"type":"error","error":{"message":"invalid x-api-key"}}'
        )
        _patch_client(monkeypatch, transport)

        with pytest.raises(TokenCountAuthError) as caught:
            await count_anthropic_context("bad-key", "claude-x", _messages())

        message = str(caught.value)
        assert "API key" in message, message
        assert "401" in message, message
