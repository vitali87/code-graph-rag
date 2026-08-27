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

from codebase_rag import constants as cs

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
        from codebase_rag import main as main_mod

        warnings: list[str] = []
        monkeypatch.setattr(
            main_mod.logger, "warning", lambda msg, *a, **k: warnings.append(str(msg))
        )
        monkeypatch.setattr(
            main_mod.logger, "debug", lambda msg, *a, **k: None
        )

        async def boom(*_args: object, **_kw: object) -> int:
            raise TokenCountAuthError("the Anthropic API key was rejected (401)")

        monkeypatch.setattr(
            "codebase_rag.services.anthropic_token_counter.count_anthropic_context",
            boom,
        )
        main_mod.app_context.session.token_auth_warned = False

        class _Config:
            provider = cs.Provider.ANTHROPIC
            api_key = "bad-key"
            model_id = "claude-x"

        monkeypatch.setattr(
            type(main_mod.settings),
            "active_orchestrator_config",
            property(lambda self: _Config()),
        )

        await main_mod._refresh_context_tokens(_messages())
        await main_mod._refresh_context_tokens(_messages())

        assert len(warnings) == 1, (
            f"expected exactly one warning per session, got {len(warnings)}: "
            f"{warnings}"
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
        from codebase_rag import main as main_mod

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

        class _Config:
            provider = cs.Provider.ANTHROPIC
            api_key = "good-key"
            model_id = "claude-x"

        monkeypatch.setattr(
            type(main_mod.settings),
            "active_orchestrator_config",
            property(lambda self: _Config()),
        )

        await main_mod._refresh_context_tokens(_messages())

        assert warnings == [], (
            f"a transient failure warned the user: {warnings}"
        )
