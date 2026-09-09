"""The context counter must work for every provider, not only Anthropic (#1500).

`prune_old_tool_results` is triggered from a percentage derived from
`session.context_tokens`, and only `_refresh_context_tokens` ever writes that
value. It returned early unless the provider was Anthropic AND an API key was
set, so `context_tokens` stayed 0 for OpenAI, Gemini, a local model, and for
Anthropic with no key -- which makes the percentage 0, which is below every
threshold, which means compaction never runs.

The bug is silent and in the reassuring direction: the status line reads 0%,
which looks like plenty of headroom rather than like a counter that never ran.
This is the [[absence-vs-not-run]] shape -- "no context used" and "context
never measured" rendered identically.

The fix keeps the Anthropic path exactly as it was (an exact count from the
provider beats an estimate) and adds a local tiktoken estimate for everyone
else. The estimate is already trusted elsewhere in this codebase: it is what
`QUERY_RESULT_MAX_TOKENS` and the pruning floor are measured in.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

from codebase_rag import constants as cs


def _messages(text: str = "hello " * 400) -> list[ModelMessage]:
    """Enough text that a working counter cannot report 0.

    The assertion below is `> 0`, so a fixture whose true count is 0 would
    pass against broken code as readily as against fixed code. 400 words is
    unambiguously non-zero under any tokeniser.
    """
    return [ModelRequest(parts=[UserPromptPart(content=text)])]


def _use_config(
    monkeypatch: pytest.MonkeyPatch, provider: object, api_key: str | None
) -> None:
    from codebase_rag import main as main_mod

    class _Config:
        pass

    _Config.provider = provider  # type: ignore[attr-defined]
    _Config.api_key = api_key  # type: ignore[attr-defined]
    _Config.model_id = "some-model"  # type: ignore[attr-defined]

    monkeypatch.setattr(
        type(main_mod.settings),
        "active_orchestrator_config",
        property(lambda self: _Config()),
    )


class TestTheCounterRunsForEveryProvider:
    @pytest.mark.asyncio
    async def test_a_non_anthropic_provider_gets_a_context_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect itself: OpenAI left the counter at 0 forever.

        With the counter stuck at 0 the derived percentage is 0, so the
        critical threshold is never crossed and `prune_old_tool_results` is
        never called. Compaction was Anthropic-only without saying so.
        """
        from codebase_rag import main as main_mod

        main_mod.app_context.session.context_tokens = 0
        _use_config(monkeypatch, cs.Provider.OPENAI, "sk-whatever")

        await main_mod._refresh_context_tokens(_messages())

        assert main_mod.app_context.session.context_tokens > 0, (
            "the context counter stayed at 0 for a non-Anthropic provider, so "
            "the pruning threshold can never be crossed"
        )

    @pytest.mark.asyncio
    async def test_anthropic_without_a_key_gets_a_context_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second half of the same early return.

        `provider != ANTHROPIC or not api_key` has two ways to fire. Testing
        only the provider half would leave a keyless Anthropic session with
        the original bug and a green suite.
        """
        from codebase_rag import main as main_mod

        main_mod.app_context.session.context_tokens = 0
        _use_config(monkeypatch, cs.Provider.ANTHROPIC, None)

        await main_mod._refresh_context_tokens(_messages())

        assert main_mod.app_context.session.context_tokens > 0, (
            "a keyless Anthropic session cannot reach the remote counter and "
            "was left with no count at all"
        )

    @pytest.mark.asyncio
    async def test_the_estimate_tracks_the_size_of_the_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A constant would satisfy `> 0` above while measuring nothing.

        This is the discriminating assertion: the count has to be a function
        of the input. A stub returning 1, or a counter reading a stale value
        from a previous call, passes both tests above and fails this one.
        """
        from codebase_rag import main as main_mod

        _use_config(monkeypatch, cs.Provider.OPENAI, "sk-whatever")

        main_mod.app_context.session.context_tokens = 0
        await main_mod._refresh_context_tokens(_messages("hi " * 50))
        small = main_mod.app_context.session.context_tokens

        main_mod.app_context.session.context_tokens = 0
        await main_mod._refresh_context_tokens(_messages("hi " * 5000))
        large = main_mod.app_context.session.context_tokens

        assert small > 0 and large > small * 10, (
            f"the estimate does not scale with the history: {small} then {large}"
        )

    @pytest.mark.asyncio
    async def test_anthropic_with_a_key_still_uses_the_exact_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The control against over-correcting.

        The obvious wrong fix is to replace the remote count with the local
        estimate everywhere. The provider's own count is exact and accounts
        for tool definitions and system prompt overhead that tiktoken over a
        message list cannot see, so the Anthropic path must be untouched.

        Asserts a sentinel the estimate could never produce.
        """
        from codebase_rag import main as main_mod

        sentinel = 1234567

        async def exact(*_args: object, **_kw: object) -> int:
            return sentinel

        monkeypatch.setattr(
            "codebase_rag.services.anthropic_token_counter.count_anthropic_context",
            exact,
        )
        main_mod.app_context.session.context_tokens = 0
        _use_config(monkeypatch, cs.Provider.ANTHROPIC, "sk-real-key")

        await main_mod._refresh_context_tokens(_messages())

        assert main_mod.app_context.session.context_tokens == sentinel, (
            "the exact provider count was replaced by the local estimate"
        )

    @pytest.mark.asyncio
    async def test_a_failed_remote_count_falls_back_rather_than_reporting_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rate-limited Anthropic session must not read as empty context.

        Before this, any exception left `context_tokens` at whatever it was
        and logged at debug. On the first turn that is 0, so a transient
        failure produced the same silent no-compaction state as the missing
        provider support -- for the provider that DID have support.
        """
        from codebase_rag import main as main_mod
        from codebase_rag.services.anthropic_token_counter import TokenCountError

        async def boom(*_args: object, **_kw: object) -> int:
            raise TokenCountError("503: unavailable")

        monkeypatch.setattr(
            "codebase_rag.services.anthropic_token_counter.count_anthropic_context",
            boom,
        )
        monkeypatch.setattr(main_mod.logger, "debug", lambda msg, *a, **k: None)
        main_mod.app_context.session.context_tokens = 0
        _use_config(monkeypatch, cs.Provider.ANTHROPIC, "sk-real-key")

        await main_mod._refresh_context_tokens(_messages())

        assert main_mod.app_context.session.context_tokens > 0, (
            "a transient remote failure left the counter at 0, which reads as "
            "an empty context rather than an unmeasured one"
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_config_still_produces_a_count(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third early return, found by mutation rather than by reading.

        Deleting the write on this path left every other test green. The
        estimate needs only the messages, so a config that cannot be read is
        no reason to report an empty context -- and this path runs at startup,
        before a model is settled on, which is exactly when a stale 0 would
        persist longest.
        """
        from codebase_rag import main as main_mod

        def explode(self: object) -> object:
            raise RuntimeError("no orchestrator configured")

        monkeypatch.setattr(
            type(main_mod.settings),
            "active_orchestrator_config",
            property(explode),
        )
        main_mod.app_context.session.context_tokens = 0

        await main_mod._refresh_context_tokens(_messages())

        assert main_mod.app_context.session.context_tokens > 0, (
            "an unreadable config left the counter at 0 rather than falling "
            "back to the estimate, which needs no config at all"
        )


class TestEveryPartTypeIsCounted:
    """The axis the first version of this file left unvaried.

    Every test above builds `ModelRequest(parts=[UserPromptPart(...)])` -- the
    one shape the estimator handled correctly -- so all six passed while tool
    calls counted as zero. A suite that only ever exercises the working shape
    cannot see the defect, however many assertions it makes (greptile-local,
    #1500).
    """

    def test_a_tool_call_is_not_free(self) -> None:
        """`ToolCallPart` carries its payload in `args`, not `content`.

        It is the only part type with no `content` field, so a loop reading
        `content` alone scored it 0. Tool arguments carry whole file bodies
        on this codebase, so this is the bulk of a long session's context,
        not an edge case.
        """
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        from codebase_rag.utils.token_utils import (
            count_tokens,
            estimate_message_tokens,
        )

        body = "x = 1\n" * 5000
        part = ToolCallPart(
            tool_name="create_new_file",
            args={"file_path": "big.py", "content": body},
        )
        counted = estimate_message_tokens([ModelResponse(parts=[part])])

        assert counted > count_tokens(body) * 0.5, (
            f"a tool call carrying {len(body)} characters of file content was "
            f"counted as {counted} tokens, so a history of file writes reads "
            "as an almost empty context and never triggers compaction"
        )

    def test_no_part_type_counts_as_zero(self) -> None:
        """A sweep, so the next part type added cannot silently score zero.

        Written as an enumeration rather than one assertion per type: the
        failure mode is a NEW shape whose payload field nobody thought about,
        and a test naming today's types would not catch it either -- but it
        will catch a change to any of them, and it names the field each one
        was counted through.
        """
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            RetryPromptPart,
            SystemPromptPart,
            TextPart,
            ToolCallPart,
            ToolReturnPart,
            UserPromptPart,
        )

        from codebase_rag.utils.token_utils import estimate_message_tokens

        filler = "token " * 300
        cases = {
            "UserPromptPart": ModelRequest(parts=[UserPromptPart(content=filler)]),
            "SystemPromptPart": ModelRequest(parts=[SystemPromptPart(content=filler)]),
            "TextPart": ModelResponse(parts=[TextPart(content=filler)]),
            "ToolCallPart": ModelResponse(
                parts=[ToolCallPart(tool_name="t", args={"a": filler})]
            ),
            "ToolReturnPart": ModelRequest(
                parts=[ToolReturnPart(tool_name="t", content=filler, tool_call_id="c1")]
            ),
            "RetryPromptPart": ModelRequest(
                parts=[RetryPromptPart(content=filler, tool_call_id="c2")]
            ),
        }

        zero = {name: estimate_message_tokens([m]) for name, m in cases.items()}
        assert all(count > 0 for count in zero.values()), (
            f"some part types contribute nothing to the context estimate: "
            f"{ {n: c for n, c in zero.items() if c == 0} }"
        )
