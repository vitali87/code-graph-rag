# Compaction visibility and opt-out (issue #1500). Stage one drops old tool
# output silently: nothing tells the user it happened and nothing turns it off.
# A user who cannot see that the history was compacted cannot diagnose why the
# agent stopped remembering something, and a discarding mechanism with no
# opt-out is one they cannot decline.

from __future__ import annotations

from pathlib import Path

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from codebase_rag.context_pruning import (
    DEFAULT_MINIMUM_RECOVERED_TOKENS,
    DEFAULT_PROTECT_RECENT_TOKENS,
    PRUNED_PLACEHOLDER,
    PruneReport,
    _prunable_candidates,
    describe_prune,
    prune_old_tool_results,
)
from codebase_rag.utils.token_utils import count_tokens


def _turn(question: str, tool_output: str) -> list[ModelRequest | ModelResponse]:
    call_id = f"call-{abs(hash(question)) % 10_000}"
    return [
        ModelRequest(parts=[UserPromptPart(content=question)]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="query", args={}, tool_call_id=call_id)]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="query", content=tool_output, tool_call_id=call_id
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=f"answer to {question}")]),
    ]


def _history(turns: int, output_tokens: int) -> list[ModelRequest | ModelResponse]:
    """A history of `turns` turns, each carrying ~`output_tokens` of tool output.

    Sizing matters and is easy to get wrong: the pruner protects the most
    recent `DEFAULT_PROTECT_RECENT_TOKENS` (40k) of tool output and then
    refuses unless the REMAINDER clears `DEFAULT_MINIMUM_RECOVERED_TOKENS`
    (20k). So a history needs comfortably more than 60k of tool output before
    it prunes at all -- 12 turns x 4k output is 48k, which the window alone
    absorbs, and the pruner correctly declines. `_prunes()` below asserts the
    fixture is big enough rather than leaving it to arithmetic.
    """
    blob = "result " * output_tokens
    messages: list[ModelRequest | ModelResponse] = []
    for index in range(turns):
        messages.extend(_turn(f"q{index}", blob))
    return messages


def _prunable_history() -> list[ModelRequest | ModelResponse]:
    """A history that definitely prunes, verified rather than assumed."""
    messages = _history(turns=30, output_tokens=4_000)
    candidates, recoverable = _prunable_candidates(
        messages, DEFAULT_PROTECT_RECENT_TOKENS
    )
    assert recoverable >= DEFAULT_MINIMUM_RECOVERED_TOKENS, (
        f"fixture too small to prune: {recoverable} recoverable tokens against "
        f"a {DEFAULT_MINIMUM_RECOVERED_TOKENS} floor; every assertion about a "
        f"prune would pass vacuously"
    )
    assert candidates, "fixture produced no prunable candidates"
    return messages


class TestPruneReport:
    """`describe_prune` compares before and after, leaving the pruner's own
    signature alone: `main` assigns through `message_history[:]`, and a test
    in `test_context_pruning.py` requires exactly that form."""

    def test_reports_what_it_dropped(self) -> None:
        """The caller cannot tell the user anything it is not told."""
        messages = _prunable_history()

        pruned = prune_old_tool_results(messages)
        report = describe_prune(messages, pruned)

        assert isinstance(report, PruneReport)
        assert report.pruned is True
        assert report.dropped_parts > 0
        assert report.recovered_tokens > 0

    def test_reports_when_it_declined(self) -> None:
        """A refusal below the floor is a distinct outcome, not an absence.

        `pruned=False` with zero counts must be distinguishable from a prune
        that ran: otherwise a caller cannot tell "nothing to do" from "did
        nothing", and would announce a compaction that never happened.
        """
        messages = _history(turns=1, output_tokens=10)

        pruned = prune_old_tool_results(messages)
        report = describe_prune(messages, pruned)

        assert report.pruned is False
        assert report.dropped_parts == 0
        assert report.recovered_tokens == 0

    def test_recovered_tokens_reflects_the_real_saving(self) -> None:
        """The placeholder is kept, so it is not recovered.

        A gross count would overstate the saving by one placeholder per part,
        which is exactly the error the pruner's own floor guards against.
        """
        messages = _prunable_history()
        before = sum(
            count_tokens(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart) and isinstance(part.content, str)
        )

        pruned = prune_old_tool_results(messages)
        report = describe_prune(messages, pruned)

        after = sum(
            count_tokens(part.content)
            for message in pruned
            for part in message.parts
            if isinstance(part, ToolReturnPart) and isinstance(part.content, str)
        )
        assert report.recovered_tokens == before - after

    def test_dropped_parts_counts_parts_not_messages(self) -> None:
        """Two results in one message are two drops, not one."""
        messages = _prunable_history()

        pruned = prune_old_tool_results(messages)
        report = describe_prune(messages, pruned)

        placeholders = sum(
            1
            for message in pruned
            for part in message.parts
            if getattr(part, "content", None) == PRUNED_PLACEHOLDER
        )
        assert report.dropped_parts == placeholders


class TestOptOut:
    def test_disabled_returns_the_history_untouched(self) -> None:
        """A user who turns compaction off must keep every tool result."""
        messages = _prunable_history()

        pruned = prune_old_tool_results(messages, enabled=False)

        assert pruned is messages
        assert not any(
            getattr(part, "content", None) == PRUNED_PLACEHOLDER
            for message in pruned
            for part in message.parts
        )

    def test_disabled_beats_a_history_that_would_otherwise_prune(self) -> None:
        """The opt-out is checked before the floor, not after it.

        Verified against the same history that DOES prune when enabled, so a
        green here cannot come from a history too small to trigger.
        """
        messages = _prunable_history()
        enabled = prune_old_tool_results(messages, enabled=True)
        assert describe_prune(messages, enabled).pruned is True

        disabled = prune_old_tool_results(messages, enabled=False)
        assert describe_prune(messages, disabled).pruned is False


class TestCallSite:
    """`main.py` must announce a prune and honour the setting.

    Parsed from the shipped source rather than mocked: the value of this
    feature is that the user SEES compaction, and a test that drives the
    pruner directly proves nothing about whether `main` ever tells them.
    """

    @staticmethod
    def _main_source() -> str:
        return (Path(__file__).resolve().parents[1] / "main.py").read_text()

    def test_call_site_describes_the_prune(self) -> None:
        """Without `describe_prune`, `main` has nothing to announce."""
        source = self._main_source()

        assert "describe_prune" in source, (
            "main.py must call describe_prune to learn what was dropped"
        )

    def test_call_site_announces_the_prune(self) -> None:
        source = self._main_source()

        assert "COMPACTION_NOTICE" in source, (
            "main.py must print a notice when it compacts the history"
        )

    def test_call_site_reads_the_opt_out_setting(self) -> None:
        source = self._main_source()

        assert "CONTEXT_COMPACTION_ENABLED" in source, (
            "main.py must pass the opt-out setting to the pruner"
        )
