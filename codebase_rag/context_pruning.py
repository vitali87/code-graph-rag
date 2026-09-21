"""Drop old tool output from a conversation history (issue #1500).

`message_history` accumulates for the lifetime of a session: messages are
appended and passed whole to every subsequent request, so nothing bounds the
total even though `QUERY_RESULT_MAX_TOKENS` bounds each individual result.

This is the cheap first stage of compaction, and deliberately only that. Tool
output is usually the bulk of a long session's context and the most
reproducible part of it -- a query can be run again, whereas the user's stated
goal cannot be recovered once dropped. So tool results are emptied oldest-first
and every user and assistant message is left untouched.

Summarising the dialogue is a separate stage that this does NOT do. The two
compose in that order: prune first because it costs nothing, and summarise only
if pruning could not free enough. Codex's own documentation warns that repeated
summarisation compounds accuracy loss, which is a reason to reach for it second
rather than first.

The part is emptied rather than removed. A tool call whose result has vanished
is an orphan, and providers reject that history -- `main._cancel_orphaned_tool_calls`
exists because that shape breaks a request.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .utils.token_utils import count_tokens

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

# What replaces dropped output. Says the result existed and can be re-fetched,
# rather than leaving an empty string the model might read as "the query
# returned nothing" -- an absence that means something quite different.
PRUNED_PLACEHOLDER = (
    "[earlier tool result dropped to save context; re-run the tool if needed]"
)

# Recent tool output is what the current turn is reasoning about, so it is off
# limits regardless of how much pruning would recover.
DEFAULT_PROTECT_RECENT_TOKENS = 40_000

# Below this, pruning is not worth its own cost: it invalidates the prompt
# cache for the whole prefix it rewrites, so recovering a few hundred tokens
# every turn would thrash near the limit and buy almost nothing.
DEFAULT_MINIMUM_RECOVERED_TOKENS = 20_000


def _is_prunable_tool_return(part: object) -> bool:
    """A tool return whose content is free text and may be replaced.

    Matching `BaseToolReturnPart` is right for FINDING tool results and wrong
    for OVERWRITING them, and the two are not the same question. Three of its
    five subclasses carry structured content behind typed accessors, so a
    string placeholder corrupts them. Measured, not assumed:

        ToolSearchReturnPart.discovered_tools -> TypeError:
            string indices must be integers, not 'str'
        LoadCapabilityReturnPart.instructions -> AttributeError:
            'str' object has no attribute 'get'

    So this names the two subclasses typed `ToolReturnContent`, and an unknown
    future subclass is skipped rather than corrupted.

    The class is necessary and not sufficient. `ToolReturnContent` admits a
    string but not only a string: a `ToolReturnPart` accepts a dict or a list
    without complaint, and the placeholder would destroy that value rather
    than shrink it. Narrowing to the right class is not narrowing to the right
    type, and the second check is what the first one missed.

    Both directions are the safe one for a mechanism whose entire value is
    that the history it compacts still works: recovering less is a cost,
    corrupting the conversation is a defect (CodeRabbit, #1506).
    """
    from pydantic_ai.messages import NativeToolReturnPart, ToolReturnPart

    if type(part) not in (ToolReturnPart, NativeToolReturnPart):
        return False
    return isinstance(getattr(part, "content", None), str)


def _content_tokens(part: object) -> int:
    content = getattr(part, "content", None)
    if content is None:
        return 0
    return count_tokens(content if isinstance(content, str) else str(content))


def _prunable_candidates(
    messages: list[ModelMessage], protect_recent_tokens: int
) -> tuple[list[tuple[int, int]], int]:
    """Locate prunable parts outside the protected window, newest-first.

    Returns `(message index, part index)` pairs and the tokens pruning them
    would actually recover. Walking backwards is what makes the protected
    window mean "the most recent N tokens of tool output" rather than a count
    of messages: the newest results fill it and everything older is a
    candidate.

    Split out of `prune_old_tool_results` so selection and rebuilding are
    separately readable -- the two do genuinely different work, and combining
    them put four conditions inside two nested loops (SonarCloud S3776).
    """
    candidates: list[tuple[int, int]] = []
    protected_tokens = 0
    recoverable_tokens = 0
    # The placeholder is kept, so it is not recovered. Counting gross tokens
    # would clear the floor on a rewrite that frees less than the floor asks
    # for, which is the exact thrash the floor exists to prevent.
    placeholder_tokens = count_tokens(PRUNED_PLACEHOLDER)

    for message_index in range(len(messages) - 1, -1, -1):
        parts = getattr(messages[message_index], "parts", None)
        if not parts:
            continue
        for part_index in range(len(parts) - 1, -1, -1):
            part = parts[part_index]
            if not _is_prunable_tool_return(part):
                continue
            if getattr(part, "content", None) == PRUNED_PLACEHOLDER:
                continue
            part_tokens = _content_tokens(part)
            if protected_tokens < protect_recent_tokens:
                protected_tokens += part_tokens
                continue
            candidates.append((message_index, part_index))
            recoverable_tokens += max(0, part_tokens - placeholder_tokens)
    return candidates, recoverable_tokens


@dataclass(frozen=True)
class PruneReport:
    """What a prune actually did, so the caller can say so.

    `pruned=False` with zero counts is a DISTINCT outcome from a prune that
    ran: a caller that cannot tell "declined, below the floor" from "dropped
    nothing" would announce a compaction that never happened, which is worse
    than staying silent.
    """

    pruned: bool
    dropped_parts: int
    recovered_tokens: int


def describe_prune(
    before: list[ModelMessage], after: list[ModelMessage]
) -> PruneReport:
    """Compare a history with its pruned form.

    Deliberately NOT folded into `prune_old_tool_results`'s return value.
    `main` assigns through `message_history[:]` because callers hold that same
    list, and a test pins that exact form; returning a report instead would
    force every one of its call sites to change and prune a copy if any were
    missed. Adding a function beats redefining what an existing one returns.

    The comparison, not the identity check, is what decides. `after is before`
    short-circuits a DIRECT caller that passes the pruner's return value
    straight back, but `main` assigns through `message_history[:]` against a
    separate snapshot, so those two are never the same object and this always
    walks. That is the correct outcome either way: a declined prune places no
    new placeholders, so the walk reports `pruned=False` as surely as the
    shortcut would. The notice cannot fire on a prune that did not happen.
    """
    if after is before:
        return PruneReport(pruned=False, dropped_parts=0, recovered_tokens=0)

    dropped = 0
    recovered = 0
    for old_message, new_message in zip(before, after, strict=False):
        old_parts = getattr(old_message, "parts", None) or ()
        new_parts = getattr(new_message, "parts", None) or ()
        for old_part, new_part in zip(old_parts, new_parts, strict=False):
            old_content = getattr(old_part, "content", None)
            new_content = getattr(new_part, "content", None)
            if new_content == PRUNED_PLACEHOLDER and old_content != PRUNED_PLACEHOLDER:
                dropped += 1
                # Clamp per part, matching `_prunable_candidates`. The
                # placeholder is 17 tokens, so a result shorter than that
                # COSTS tokens to prune ('ok' -> -16). Summing raw deltas
                # lets enough short results swamp the genuine saving and the
                # total goes negative: "freeing ~-320 tokens" reached the
                # user on a 5000-short-result history (greptile-local).
                recovered += max(
                    0, _content_tokens(old_part) - _content_tokens(new_part)
                )
    return PruneReport(
        pruned=dropped > 0, dropped_parts=dropped, recovered_tokens=recovered
    )


def prune_old_tool_results(
    messages: list[ModelMessage],
    protect_recent_tokens: int = DEFAULT_PROTECT_RECENT_TOKENS,
    minimum_recovered_tokens: int = DEFAULT_MINIMUM_RECOVERED_TOKENS,
    enabled: bool = True,
) -> list[ModelMessage]:
    """Empty tool results outside the protected window, oldest first.

    Walks backwards accumulating tool-output tokens, so the newest results fill
    the protected window and everything older becomes a candidate. Returns the
    history unchanged when the candidates are not worth the cache invalidation.

    Already-pruned parts are not counted as recoverable: the placeholder cannot
    be freed twice, and counting it would let a long session clear the floor
    every turn on tokens it cannot actually reclaim.
    """
    # Checked BEFORE the floor and before any scanning: a user who turned
    # compaction off has declined the mechanism, not asked for a cheaper one.
    if not enabled:
        return messages

    candidates, recoverable_tokens = _prunable_candidates(
        messages, protect_recent_tokens
    )
    if recoverable_tokens < minimum_recovered_tokens:
        return messages

    # Rebuild only the messages that actually change, so untouched messages
    # keep their identity and the caller can tell what moved.
    by_message: dict[int, set[int]] = {}
    for message_index, part_index in candidates:
        by_message.setdefault(message_index, set()).add(part_index)

    pruned: list[ModelMessage] = []
    for message_index, message in enumerate(messages):
        prune_indices = by_message.get(message_index)
        if not prune_indices:
            pruned.append(message)
            continue
        new_parts = [
            replace(part, content=PRUNED_PLACEHOLDER)
            if part_index in prune_indices
            else part
            for part_index, part in enumerate(message.parts)
        ]
        pruned.append(replace(message, parts=new_parts))
    return pruned
