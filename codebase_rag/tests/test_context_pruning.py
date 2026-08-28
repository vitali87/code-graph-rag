# Conversation compaction, stage one (issue #1500): tool results are the bulk
# of a long session's context and the most reproducible part of it, so they are
# what gets dropped first. The dialogue is left intact; summarising it is a
# separate stage that this deliberately does not do.

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    LoadCapabilityReturnPart,
    ModelRequest,
    ModelResponse,
    NativeToolSearchReturnPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    ToolSearchReturnPart,
    UserPromptPart,
)

from codebase_rag.context_pruning import (
    PRUNED_PLACEHOLDER,
    prune_old_tool_results,
)
from codebase_rag.utils.token_utils import count_tokens


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


@pytest.mark.parametrize(
    ("part_factory", "accessor"),
    [
        (
            lambda: ToolSearchReturnPart(
                tool_name="search",
                content={"discovered_tools": [], "message": "ok"},
                tool_call_id="c-search",
            ),
            "discovered_tools",
        ),
        (
            lambda: NativeToolSearchReturnPart(
                tool_name="search",
                content={"discovered_tools": [], "message": "ok"},
                tool_call_id="c-native-search",
            ),
            "discovered_tools",
        ),
        (
            lambda: LoadCapabilityReturnPart(
                tool_name="load",
                content={"instructions": "do the thing"},
                tool_call_id="c-load",
            ),
            "instructions",
        ),
    ],
)
def test_parts_with_structured_content_are_left_alone(part_factory, accessor) -> None:
    """Only free-text tool results may be replaced with a string placeholder.

    `BaseToolReturnPart` is the right base for FINDING tool results, which is
    why the traversal matches on it. It is the wrong base for OVERWRITING
    them: three subclasses carry structured content behind typed accessors,
    and a string breaks them. Measured, not assumed:

        ToolSearchReturnPart.discovered_tools -> TypeError:
            string indices must be integers, not 'str'
        LoadCapabilityReturnPart.instructions -> AttributeError:
            'str' object has no attribute 'get'

    A pruner that corrupts the history it is compacting is worse than one
    that recovers less, so these are skipped rather than given per-subtype
    placeholders. They are framework bookkeeping and small; the free-text
    results this targets are the bulk (CodeRabbit, #1506).
    """
    part = part_factory()
    history = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c-big")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="t", content="BIG " * 5000, tool_call_id="c-big"
                ),
                part,
            ]
        ),
    ]

    pruned = prune_old_tool_results(
        history, protect_recent_tokens=0, minimum_recovered_tokens=1
    )

    survivor = next(
        p for message in pruned for p in message.parts if type(p) is type(part)
    )
    assert survivor.content == part.content, (
        f"{type(part).__name__} carries structured content; replacing it with a "
        "string corrupts the history"
    )
    getattr(survivor, accessor)


def test_free_text_results_are_still_pruned_alongside_structured_ones() -> None:
    """Skipping structured parts must not disable pruning for the rest.

    Without this, the fix above is satisfied by a pruner that gave up
    entirely -- the parametrized test cannot tell "structured parts skipped"
    from "nothing pruned at all".
    """
    history = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c-big")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="t", content="BIG " * 5000, tool_call_id="c-big"
                ),
                LoadCapabilityReturnPart(
                    tool_name="load",
                    content={"instructions": "do the thing"},
                    tool_call_id="c-load",
                ),
            ]
        ),
    ]

    pruned = prune_old_tool_results(
        history, protect_recent_tokens=0, minimum_recovered_tokens=1
    )

    plain = next(
        p for message in pruned for p in message.parts if type(p) is ToolReturnPart
    )
    assert plain.content == PRUNED_PLACEHOLDER, (
        "free-text results must still be pruned when a structured part is present"
    )


def test_the_floor_measures_net_recovery_not_gross_content() -> None:
    """The placeholder is kept, so its tokens are not recovered.

    Counting gross content against the floor lets a rewrite through that
    frees less than the floor demands, which is precisely the thrash the
    floor exists to prevent: the prefix is rebuilt, the prompt cache for it
    is invalidated, and the recovery does not cover the cost.

    Concrete boundary, measured rather than assumed: the placeholder is 17
    tokens and this result is 41, so gross recovery is 41 and net is 24. At a
    floor of exactly 41, gross accounting prunes and net accounting declines
    (CodeRabbit, #1506).
    """
    history = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c-1")]),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="t", content="OLD " * 40, tool_call_id="c-1")
            ]
        ),
    ]
    gross = count_tokens("OLD " * 40)
    assert gross > count_tokens(PRUNED_PLACEHOLDER), (
        "fixture must be bigger than the placeholder or the case is vacuous"
    )

    pruned = prune_old_tool_results(
        history, protect_recent_tokens=0, minimum_recovered_tokens=gross
    )

    assert _tool_contents(pruned) == ["OLD " * 40], (
        f"net recovery is {gross - count_tokens(PRUNED_PLACEHOLDER)} against a "
        f"floor of {gross}, so this must not prune; counting gross content "
        "would rewrite the cache prefix for less than the floor demands"
    )


@pytest.mark.parametrize(
    ("label", "content"),
    [
        ("a mapping", {"rows": [1, 2, 3], "total": 3}),
        ("a sequence", [{"path": "a.py"}, {"path": "b.py"}]),
    ],
)
def test_non_string_content_is_left_alone(label: str, content: object) -> None:
    """Narrowing to the right CLASS is not narrowing to the right TYPE.

    The previous fix restricted pruning to the two subclasses annotated
    `ToolReturnContent`, on the reasoning that this type admits a string. It
    does admit one, but not only one: a `ToolReturnPart` accepts a dict or a
    list without complaint, so the class check passes a part whose content is
    structured data and the placeholder destroys it.

    That is the same defect as the subclass bug one level down, and it
    survived the fix for it. A tool result that returned rows is not made
    smaller by being replaced with a sentence, it is made wrong, and a caller
    indexing it gets a string instead (CodeRabbit, #1506).
    """
    history = [
        ModelRequest(parts=[UserPromptPart(content="q")]),
        ModelResponse(
            parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="c-big")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="t", content="BIG " * 5000, tool_call_id="c-big"
                ),
                ToolReturnPart(
                    tool_name="structured", content=content, tool_call_id="c-struct"
                ),
            ]
        ),
    ]

    pruned = prune_old_tool_results(
        history, protect_recent_tokens=0, minimum_recovered_tokens=1
    )

    survivor = next(
        p
        for message in pruned
        for p in message.parts
        if isinstance(p, ToolReturnPart) and p.tool_call_id == "c-struct"
    )
    assert survivor.content == content, (
        f"content that is {label} is not free text; replacing it with a "
        "placeholder string destroys the value rather than shrinking it"
    )
    plain = next(
        p
        for message in pruned
        for p in message.parts
        if isinstance(p, ToolReturnPart) and p.tool_call_id == "c-big"
    )
    assert plain.content == PRUNED_PLACEHOLDER, (
        "free-text results must still be pruned when a structured one is present"
    )


def test_pruning_preserves_message_count_and_call_return_pairing() -> None:
    """Pruning must never orphan a tool call, on any history it acts on.

    pydantic-ai requires every tool-call part to have a matching tool-return
    part; `main._cancel_orphaned_tool_calls` exists to repair that shape when
    a run is cancelled mid-flight. A pruner that DROPPED result messages
    rather than emptying them would manufacture the same breakage on every
    prune, and the next `rag_agent.run` would raise.

    Distinct from `test_a_pruned_result_keeps_its_tool_call_id`, which
    compares return-part ids to themselves. This pins the two structural
    facts that test cannot see: the message count is unchanged, and every
    call id still has a return id to match it. Both are asserted on a history
    that is actually pruned, so a pruner that quietly dropped a message would
    fail here rather than pass by not acting.

    Catastrophic rather than degraded, which is why it is guarded explicitly:
    losing content costs context, losing a message breaks the next request.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)

    def _ids(messages: list[ModelRequest | ModelResponse]) -> tuple[list, list]:
        calls = [
            p.tool_call_id
            for m in messages
            for p in m.parts
            if isinstance(p, ToolCallPart)
        ]
        returns = [
            p.tool_call_id
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        ]
        return calls, returns

    calls_before, returns_before = _ids(history)
    pruned = prune_old_tool_results(
        history, protect_recent_tokens=10, minimum_recovered_tokens=1
    )
    calls_after, returns_after = _ids(pruned)

    assert [
        str(p.content) for m in pruned for p in m.parts if isinstance(p, ToolReturnPart)
    ] != [
        str(p.content)
        for m in history
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ], "fixture must actually be pruned or the invariant is asserted vacuously"

    assert len(pruned) == len(history), (
        "pruning emptied a message instead of its content; a dropped message "
        "orphans its tool call and the next agent run raises"
    )
    assert calls_after == calls_before, "tool-call parts must be untouched"
    assert returns_after == returns_before, (
        "every tool return must survive with its id so its call stays paired"
    )
    assert sorted(calls_after) == sorted(returns_after), (
        "call/return pairing broken by pruning"
    )


def test_pruning_is_idempotent_so_a_stale_high_reading_costs_nothing() -> None:
    """A repeated prune on already-pruned history must be a no-op.

    The trigger this is designed for reads `session.context_tokens`, which
    has exactly one writer: a BACKGROUND coroutine spawned with a snapshot
    copy (`main.py`, `_spawn_background(_refresh_context_tokens(list(...)))`).
    Two consequences make repeat calls inevitable rather than exceptional:

    - the measurement is asynchronous, so the value describes an earlier turn
    - on a rejected key the refresh raises before its assignment, leaving the
      previous value in place, so the count can FREEZE at a high reading and
      never come down (`TokenCountAuthError` is caught and only warned about)

    Either way the trigger can fire on every subsequent turn. That must cost
    nothing, so the pruner has to be idempotent: the second call finds only
    placeholders outside the protected window, has nothing left to recover,
    and returns the same history object rather than rebuilding it.

    Identity, not just equality: rebuilding an unchanged history would
    invalidate the prompt-cache prefix on every turn for no gain, which is
    the cost the recovery floor exists to avoid.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)

    once = prune_old_tool_results(
        history, protect_recent_tokens=10, minimum_recovered_tokens=1
    )
    assert _tool_contents(once) != _tool_contents(history), (
        "fixture must actually prune on the first pass or this is vacuous"
    )

    for repeat in range(3):
        again = prune_old_tool_results(
            once, protect_recent_tokens=10, minimum_recovered_tokens=1
        )
        assert again is once, (
            f"prune {repeat + 2} rebuilt the history with nothing to recover; "
            "a frozen or stale token count fires the trigger every turn, so a "
            "repeat prune must be free rather than re-invalidating the cache"
        )


def test_a_prune_shrinks_what_a_later_token_count_would_measure() -> None:
    """The pruned history must be smaller by the tokens the trigger reads.

    States the ordering contract the call site has to honour. The refresh
    coroutine is handed `list(message_history)` AT SPAWN TIME, so a prune
    that runs after the spawn hands it the pre-prune snapshot: the count
    written back describes history that no longer exists, and the next turn
    re-triggers on a number the prune already invalidated.

    Asserted here on the pruner's own contract -- pruning genuinely reduces
    the measurable size -- so a call site placed after the spawn contradicts
    a stated property rather than merely underperforming silently.
    """
    history = _turn("first", "OLD " * 500) + _turn("second", "NEW " * 500)

    def _measurable(messages: list[ModelRequest | ModelResponse]) -> int:
        return sum(
            count_tokens(str(p.content))
            for m in messages
            for p in m.parts
            if isinstance(p, ToolReturnPart)
        )

    before = _measurable(history)
    after = _measurable(
        prune_old_tool_results(
            history, protect_recent_tokens=10, minimum_recovered_tokens=1
        )
    )

    assert after < before, (
        f"pruning must reduce what a later token count measures ({before} -> "
        f"{after}); if it does not, the trigger reading that count can never "
        "clear and will fire on every turn"
    )
