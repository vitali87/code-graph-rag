# Conversation compaction, stage one (issue #1500): tool results are the bulk
# of a long session's context and the most reproducible part of it, so they are
# what gets dropped first. The dialogue is left intact; summarising it is a
# separate stage that this deliberately does not do.

from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from codebase_rag.context_pruning import (
    PRUNED_PLACEHOLDER,
    prune_old_tool_results,
)


def _turn(question: str, tool_output: str) -> list[ModelRequest | ModelResponse]:
    """One user turn: question, a tool call, its result, and an answer."""
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


def _tool_contents(messages: list[ModelRequest | ModelResponse]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _user_prompts(messages: list[ModelRequest | ModelResponse]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


def test_the_oldest_tool_result_is_replaced_and_the_newest_is_kept() -> None:
    """Pruning is oldest-first, which is the whole premise of the strategy.

    Recent tool output is what the current turn is reasoning about; old tool
    output has usually already been folded into an answer and can be
    re-fetched. Dropping the newest first would break the turn in progress.
    """
    history = _turn("first", "OLD RESULT " * 200) + _turn("second", "NEW RESULT " * 200)

    # The floor is a separate guard with its own test; lower it here so this
    # test measures ordering alone. Left at its default it would block the
    # prune and this would assert an outcome its own parameters forbid.
    pruned = prune_old_tool_results(
        history, protect_recent_tokens=100, minimum_recovered_tokens=1
    )

    contents = _tool_contents(pruned)
    assert contents[0] == PRUNED_PLACEHOLDER, (
        "the oldest tool result should have been replaced, but it was kept"
    )
    assert "NEW RESULT" in contents[-1], (
        "the newest tool result is inside the protected window and must survive"
    )


def test_the_dialogue_survives_pruning() -> None:
    """Only tool output is dropped. The conversation itself is the point.

    This is what separates strategy (3) from a sliding window: the user's
    stated goal usually appears in their FIRST message, which a window drops
    and this must not.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)

    pruned = prune_old_tool_results(history, protect_recent_tokens=10)

    assert _user_prompts(pruned) == ["first", "second"], (
        "user messages must survive pruning; losing them loses the goal"
    )
    texts = [
        str(part.content)
        for message in pruned
        for part in message.parts
        if isinstance(part, TextPart)
    ]
    assert texts == ["answer to first", "answer to second"], (
        "assistant replies must survive pruning"
    )


def test_a_pruned_result_keeps_its_tool_call_id() -> None:
    """The part is emptied, never removed.

    A tool call whose result vanishes is an orphan, and providers reject that
    history. `main._cancel_orphaned_tool_calls` exists precisely because this
    shape breaks a request, so pruning must not manufacture it.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)
    call_ids_before = [
        part.tool_call_id
        for message in history
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]

    pruned = prune_old_tool_results(history, protect_recent_tokens=10)

    call_ids_after = [
        part.tool_call_id
        for message in pruned
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert call_ids_after == call_ids_before, (
        "every tool return must stay, with its id, or its call is orphaned"
    )


def test_nothing_is_pruned_when_little_would_be_recovered() -> None:
    """Below the floor, pruning is not worth its own cost.

    Without a floor the session thrashes near the limit: every turn prunes a
    few hundred tokens, invalidates the prompt cache for the whole prefix, and
    buys almost nothing. OpenCode uses the same guard for the same reason.
    """
    history = _turn("first", "tiny") + _turn("second", "tiny")

    pruned = prune_old_tool_results(
        history, protect_recent_tokens=0, minimum_recovered_tokens=20_000
    )

    assert _tool_contents(pruned) == ["tiny", "tiny"], (
        "recovering a few tokens is not worth invalidating the cache prefix"
    )


def test_an_already_pruned_result_is_not_recounted_as_recoverable() -> None:
    """Pruning twice must not report the placeholder as more to recover.

    If already-pruned parts counted toward the recoverable total, a long
    session would clear the floor on every turn using tokens it cannot
    actually free, and prune forever while recovering nothing.

    The floor is set to 1 deliberately. Left at its default, the second pass
    returns unchanged simply because the short placeholder cannot clear
    20,000 tokens, so the assertion would hold whether or not the skip
    existed -- true of the working AND the broken code, which measures
    nothing. At a floor of 1 the only thing keeping the second pass inert is
    the skip itself.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)

    once = prune_old_tool_results(
        history, protect_recent_tokens=10, minimum_recovered_tokens=1
    )
    twice = prune_old_tool_results(
        once, protect_recent_tokens=10, minimum_recovered_tokens=1
    )

    assert _tool_contents(twice) == _tool_contents(once), (
        "a second pass over already-pruned history should change nothing"
    )
    assert twice is once, (
        "with nothing left to recover the history should be returned as-is, "
        "not rebuilt -- rebuilding invalidates the prompt cache for no gain"
    )


def test_history_without_tool_results_is_returned_unchanged() -> None:
    """Nothing to prune is not an error, and must not touch the dialogue."""
    history = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(parts=[TextPart(content="hi")]),
    ]

    pruned = prune_old_tool_results(history, protect_recent_tokens=0)

    assert _user_prompts(pruned) == ["hello"]
    assert len(pruned) == len(history)
