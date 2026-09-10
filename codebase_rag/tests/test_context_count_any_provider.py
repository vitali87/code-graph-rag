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

        # Split because the two halves fail for different reasons: the first
        # says the estimator produced nothing at all, the second that it
        # produced a constant. A composite assertion reports both as one
        # message and leaves the reader to work out which (SonarCloud S9073).
        assert small > 0, (
            f"the estimator returned {small} for a non-empty history, so it "
            "measured nothing rather than measuring something small"
        )
        assert large > small * 10, (
            f"a history 100x larger estimated {large} against {small}, so the "
            "count does not scale with the history and is effectively a constant"
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


class TestTheRefreshDoesNotBlockTheEventLoop:
    """The estimate must not freeze the UI while it runs (#1832 review).

    A coroutine runs synchronously until its first await, so tokenising the
    history inline stalled the interactive loop for as long as it took --
    measured at 3.3 seconds on a twelve-message tool-call history, with every
    keystroke and spinner frozen. This is a background refresh feeding a
    status line; it must never be why the UI waits.
    """

    @pytest.mark.asyncio
    async def test_a_large_history_does_not_stall_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Measures the LOOP, not the function.

        Asserting the refresh is fast would be the wrong test: the work is
        genuinely slow and is allowed to be. What must not happen is other
        tasks being starved while it runs, so this times a concurrent ticker
        and asserts its worst gap stays small.

        The threshold is deliberately loose. A real regression here is
        seconds, so the gap between pass and fail is two orders of magnitude
        -- this is not a test that goes red because a laptop was busy.
        """
        import asyncio
        import time

        from codebase_rag import main as main_mod

        slow_calls = 0

        def slow_estimate(messages: object) -> int:
            nonlocal slow_calls
            slow_calls += 1
            time.sleep(0.6)
            return 4321

        monkeypatch.setattr(main_mod, "estimate_message_tokens", slow_estimate)
        _use_config(monkeypatch, cs.Provider.OPENAI, "sk-whatever")
        main_mod.app_context.session.context_tokens = 0

        stop = asyncio.Event()
        worst = 0.0

        async def ticker() -> None:
            nonlocal worst
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                worst = max(worst, now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await main_mod._refresh_context_tokens(_messages())
        stop.set()
        await task

        assert slow_calls == 1, (
            "fixture guard: the estimator must actually have run, or a gap of "
            "zero proves nothing"
        )
        assert main_mod.app_context.session.context_tokens == 4321, (
            "fixture guard: the estimate must have been written back"
        )
        assert worst < 0.3, (
            f"the event loop was starved for {worst:.3f}s while the context "
            "estimate ran, so the whole UI freezes on a long history"
        )

    @pytest.mark.asyncio
    async def test_the_no_config_path_also_stays_off_the_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The second call site, which the test above does not reach.

        `_refresh_context_tokens` estimates on TWO paths: the configured one,
        and the fallback when the config cannot be read at all. Mutating the
        fallback to run inline left every other test green -- it is only
        reached when reading the config raises, which nothing else here does.

        That path runs at startup, before a model is settled on, so blocking
        the loop there freezes the UI at exactly the moment a user is waiting
        for the first prompt.
        """
        import asyncio
        import time

        from codebase_rag import main as main_mod

        def slow(_messages: object) -> int:
            time.sleep(0.6)
            return 4321

        monkeypatch.setattr(main_mod, "estimate_message_tokens", slow)

        def explode(self: object) -> object:
            raise RuntimeError("no orchestrator configured")

        monkeypatch.setattr(
            type(main_mod.settings), "active_orchestrator_config", property(explode)
        )
        main_mod.app_context.session.context_tokens = 0

        stop = asyncio.Event()
        worst = 0.0

        async def ticker() -> None:
            nonlocal worst
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                worst = max(worst, now - last)
                last = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0.05)
        await main_mod._refresh_context_tokens(_messages())
        stop.set()
        await task

        assert main_mod.app_context.session.context_tokens == 4321, (
            "fixture guard: the fallback estimate must have run and been "
            "written back, or a small gap proves nothing"
        )
        assert worst < 0.3, (
            f"the event loop was starved for {worst:.3f}s on the no-config "
            "path, which runs at startup while the user waits"
        )

    @pytest.mark.asyncio
    async def test_a_cancelled_refresh_does_not_hold_a_joined_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An abandoned estimate must not delay interpreter shutdown.

        Moving the estimate off the loop fixed the UI freeze but introduced a
        second cost: `asyncio.to_thread` uses an executor whose atexit hook
        JOINS its threads, so a cancelled refresh still made shutdown wait out
        an in-flight estimate -- seconds on a large history (Greptile, #1832).

        The estimate now runs on a daemon thread, which is never joined. This
        asserts the two properties that make that safe and observable: the
        cancellation returns promptly rather than waiting for the work, and
        the thread doing the work is a daemon.
        """
        import asyncio
        import threading
        import time

        from codebase_rag import main as main_mod

        started = threading.Event()
        seen: dict[str, bool] = {}

        def slow(_messages: object) -> int:
            seen["daemon"] = threading.current_thread().daemon
            started.set()
            time.sleep(1.0)
            return 99

        monkeypatch.setattr(main_mod, "estimate_message_tokens", slow)
        _use_config(monkeypatch, cs.Provider.OPENAI, "sk-whatever")

        task = asyncio.create_task(main_mod._refresh_context_tokens(_messages()))
        await asyncio.to_thread(started.wait, 5.0)
        assert started.is_set(), (
            "fixture guard: the estimate never started, so cancelling it proves nothing"
        )

        began = time.perf_counter()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.perf_counter() - began

        assert elapsed < 0.5, (
            f"cancelling the refresh waited {elapsed:.2f}s for the estimate "
            "to finish, so shutdown blocks on abandoned work"
        )
        assert seen.get("daemon") is True, (
            "the estimate ran on a non-daemon thread, which the interpreter "
            "joins at exit however promptly the await returned"
        )
