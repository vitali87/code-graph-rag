# Compaction visibility and opt-out (issue #1500). Stage one drops old tool
# output silently: nothing tells the user it happened and nothing turns it off.
# A user who cannot see that the history was compacted cannot diagnose why the
# agent stopped remembering something, and a discarding mechanism with no
# opt-out is one they cannot decline.

from __future__ import annotations

import ast
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

    def test_recovered_tokens_is_never_negative(self) -> None:
        """A result shorter than the placeholder costs tokens to prune.

        The placeholder is ~17 tokens, so pruning `'ok'` (1 token) LOSES 16.
        Summing raw deltas let enough short results swamp the genuine saving:
        a history of 5000 short results plus 30 large ones reported
        `recovered_tokens=-320`, and the notice read "freeing ~-320 tokens".
        Clamping per part matches how `_prunable_candidates` already counts.
        """
        messages: list[ModelRequest | ModelResponse] = []
        for index in range(5_000):
            messages.extend(_turn(f"tiny{index}", "ok"))
        for index in range(30):
            messages.extend(_turn(f"big{index}", "result " * 4_000))

        pruned = prune_old_tool_results(messages)
        report = describe_prune(messages, pruned)

        assert report.pruned is True
        assert report.recovered_tokens >= 0
        # And it must still report the real saving from the large results,
        # not collapse to zero: a clamp that zeroed everything would also
        # satisfy `>= 0`.
        assert report.recovered_tokens > 50_000

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

    Parsed as an AST, not grepped. Substring presence is satisfied by code
    that never runs and by a setting that is read and discarded -- the two
    ways this feature actually dies. The earlier string-grep version of these
    tests passed with `if prune_report.pruned:` rewritten to `if False:`
    (greptile-local), which is exactly the regression they exist to catch.
    """

    @staticmethod
    def _main_tree() -> ast.Module:
        return ast.parse((Path(__file__).resolve().parents[1] / "main.py").read_text())

    @staticmethod
    def _prune_call(tree: ast.Module) -> ast.Call:
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "prune_old_tool_results"
        ]
        assert len(calls) == 1, (
            f"expected exactly one prune call site in main.py, found {len(calls)}"
        )
        return calls[0]

    def test_notice_is_printed_under_the_pruned_guard(self) -> None:
        """The print must be reachable, and only when a prune happened."""
        tree = self._main_tree()

        guarded = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and isinstance(node.test, ast.Attribute)
            and node.test.attr == "pruned"
            and any(
                isinstance(inner, ast.Attribute) and inner.attr == "COMPACTION_NOTICE"
                for inner in ast.walk(node)
            )
        ]
        assert guarded, (
            "COMPACTION_NOTICE must be printed inside `if <report>.pruned:`; "
            "a constant-false guard or an unguarded print both fail this"
        )

    def test_notice_reports_the_report_s_own_numbers(self) -> None:
        """Hard-coded or mismatched numbers would misinform the user."""
        tree = self._main_tree()

        formats = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "COMPACTION_NOTICE"
        ]
        assert formats, "COMPACTION_NOTICE must be formatted with real values"

        keywords = {kw.arg for kw in formats[0].keywords}
        assert keywords == {"parts", "tokens"}, (
            f"expected parts= and tokens= keywords, got {keywords}"
        )
        for keyword in formats[0].keywords:
            assert isinstance(keyword.value, ast.Attribute), (
                f"{keyword.arg}= must come from the PruneReport, not a literal"
            )
            assert keyword.value.attr in {"dropped_parts", "recovered_tokens"}, (
                f"{keyword.arg}= reads {keyword.value.attr}, not a report field"
            )

    def test_opt_out_gates_the_whole_block_not_just_the_pruner(self) -> None:
        """Opting out must cost nothing per turn, not just skip the prune.

        Passing `enabled=False` to the pruner alone still runs
        `list(message_history)` and a full `describe_prune` walk on every
        turn above the threshold -- 2.5ms per turn on an 8000-message
        history, paid only by users who turned the feature off, and growing
        with the history that opting out leaves unbounded (Copilot, #2106).
        """
        tree = self._main_tree()
        call = self._prune_call(tree)

        guards = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.If)
            and any(
                isinstance(inner, ast.Attribute)
                and inner.attr == "CONTEXT_COMPACTION_ENABLED"
                for inner in ast.walk(node.test)
            )
            and call in list(ast.walk(node))
        ]
        assert guards, (
            "the prune call must sit inside an `if` that tests "
            "CONTEXT_COMPACTION_ENABLED; gating only the pruner leaves the "
            "snapshot and describe_prune running for users who opted out"
        )

    def test_opt_out_is_passed_to_the_pruner(self) -> None:
        """Reading the setting is not honouring it.

        `_ = settings.CONTEXT_COMPACTION_ENABLED` next to an unchanged call
        satisfies a substring check while leaving compaction always on.
        """
        call = self._prune_call(self._main_tree())

        enabled = [kw for kw in call.keywords if kw.arg == "enabled"]
        assert enabled, (
            "prune_old_tool_results must be called with enabled=; without it "
            "the setting cannot turn compaction off"
        )
        value = enabled[0].value
        assert isinstance(value, ast.Attribute), (
            "enabled= must read the setting, not a literal"
        )
        assert value.attr == "CONTEXT_COMPACTION_ENABLED", (
            f"enabled= reads {value.attr}, not the opt-out setting"
        )

    def test_describe_prune_compares_a_pre_prune_snapshot(self) -> None:
        """Comparing the history with itself would report nothing dropped."""
        tree = self._main_tree()

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "describe_prune"
        ]
        assert len(calls) == 1, "expected exactly one describe_prune call"

        args = calls[0].args
        assert len(args) == 2, "describe_prune takes (before, after)"
        assert isinstance(args[0], ast.Name), "first argument must be the snapshot"
        assert args[0].id != "message_history", (
            "describe_prune must compare a pre-prune SNAPSHOT against the "
            "pruned history; passing message_history twice reports nothing"
        )
