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

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from codebase_rag.services.anthropic_token_counter import (
    TokenCountAuthError,
    TokenCountError,
    count_anthropic_context,
)


def _model(status: int, body: str) -> AnthropicModel:
    """An `AnthropicModel` whose every request is answered with `status`."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, text=body)

    provider = AnthropicProvider(
        api_key="key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    # No retries: a 429 or 5xx would otherwise be retried with backoff.
    provider.client.max_retries = 0
    return AnthropicModel("claude-x", provider=provider)


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
    async def test_an_auth_status_raises_the_auth_error(self, status: int) -> None:
        """401 and 403 are configuration errors, not failures to count.

        Both mean the credential is rejected. Neither is retryable, so the
        caller needs to tell the user rather than log at debug and move on.
        """
        model = _model(
            status, '{"type":"error","error":{"message":"invalid x-api-key"}}'
        )

        # Built OUTSIDE the raises block: a failure inside `_messages()`
        # would otherwise satisfy `pytest.raises` and the test would pass
        # for the wrong reason (Sonar S5778).
        messages = _messages()

        with pytest.raises(TokenCountAuthError):
            await count_anthropic_context(model, messages)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [429, 500, 503])
    async def test_a_transient_status_raises_the_plain_error(self, status: int) -> None:
        """Rate limits and server errors stay the ordinary failure.

        The control that keeps the fix from over-reaching: if every status
        became an auth error, the caller would nag about credentials during a
        rate limit, which is worse than the original defect.
        """
        model = _model(status, '{"type":"error","error":{"message":"oops"}}')

        messages = _messages()

        with pytest.raises(TokenCountError) as caught:
            await count_anthropic_context(model, messages)

        assert not isinstance(caught.value, TokenCountAuthError), (
            f"status {status} was classified as an authentication failure; "
            "it is transient and a retry may succeed"
        )

    @pytest.mark.asyncio
    async def test_the_auth_error_is_a_token_count_error(
        self,
    ) -> None:
        """Subclassing keeps existing `except TokenCountError` callers working.

        A separate exception hierarchy would silently escape any caller that
        already handles the base type -- turning a logged failure into a
        crash in a background task.
        """
        model = _model(401, "denied")

        messages = _messages()

        with pytest.raises(TokenCountError):
            await count_anthropic_context(model, messages)


class TestMessage:
    """What the user is actually told."""

    @pytest.mark.asyncio
    async def test_the_auth_message_names_the_api_key(
        self,
    ) -> None:
        """The message must say the key is invalid, not echo provider JSON.

        The report on #1493 shows the raw blob reaching the user:

            Context token count failed: 401: {"type":"error","error":...}

        A reader has to parse that to learn their key is wrong. The status
        code is retained for diagnosis, but the actionable part leads.
        """
        model = _model(401, '{"type":"error","error":{"message":"invalid x-api-key"}}')

        messages = _messages()

        with pytest.raises(TokenCountAuthError) as caught:
            await count_anthropic_context(model, messages)

        message = str(caught.value)
        assert "API key" in message, message
        assert "401" in message, message


class TestCallerBehaviour:
    """What `_refresh_context_tokens` does with each error kind."""

    @pytest.mark.asyncio
    async def test_an_auth_failure_warns_once_per_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rejected key must WARN, and only on the first refresh.

        Debug is the wrong level for a permanent failure: it recurs on every
        turn and silently disables the counter for the whole session, so the
        user never learns their key is wrong.

        Once per session, because the counter refreshes on every turn and
        repeating an unactionable-until-restart message would bury the rest
        of the log. Asserts the SECOND call is silent, which is what
        distinguishes "warned once" from "warned always".
        """
        from codebase_rag import agent_loop as main_mod

        warnings: list[str] = []
        monkeypatch.setattr(
            main_mod.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
        )
        monkeypatch.setattr(main_mod.logger, "debug", lambda msg, *a, **k: None)

        async def boom(*_args: object, **_kw: object) -> int:
            raise TokenCountAuthError("the Anthropic API key was rejected (401)")

        monkeypatch.setattr(
            "codebase_rag.services.anthropic_token_counter.count_anthropic_context",
            boom,
        )
        main_mod.app_context.session.token_auth_warned = False

        agent = Agent(_model(200, "{}"))

        await main_mod._refresh_context_tokens(_messages(), agent)
        await main_mod._refresh_context_tokens(_messages(), agent)

        assert len(warnings) == 1, (
            f"expected exactly one warning per session, got {len(warnings)}: {warnings}"
        )
        assert "API key" in warnings[0] or "rejected" in warnings[0], warnings[0]

    @pytest.mark.asyncio
    async def test_a_transient_failure_stays_at_debug(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control: an ordinary failure must NOT warn.

        Without this the fix could warn on every error kind, which is the
        original defect inverted -- nagging about credentials during a rate
        limit.
        """
        from codebase_rag import agent_loop as main_mod

        warnings: list[str] = []
        monkeypatch.setattr(
            main_mod.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
        )
        monkeypatch.setattr(main_mod.logger, "debug", lambda msg, *a, **k: None)

        async def boom(*_args: object, **_kw: object) -> int:
            raise TokenCountError("503: unavailable")

        monkeypatch.setattr(
            "codebase_rag.services.anthropic_token_counter.count_anthropic_context",
            boom,
        )
        main_mod.app_context.session.token_auth_warned = False

        agent = Agent(_model(200, "{}"))

        await main_mod._refresh_context_tokens(_messages(), agent)

        assert warnings == [], f"a transient failure warned the user: {warnings}"
