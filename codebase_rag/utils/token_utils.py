from __future__ import annotations

import json
from collections.abc import Iterable
from functools import cache

import tiktoken
from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..types_defs import ResultRow


@cache
def _get_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(cs.TIKTOKEN_ENCODING)


def count_tokens(text: str) -> int:
    return len(_get_encoding().encode(text))


def truncate_results_by_tokens(
    results: list[ResultRow],
    max_tokens: int,
    original_total: int | None = None,
) -> tuple[list[ResultRow], int, bool]:
    if not results:
        return results, 0, False

    kept: list[ResultRow] = []
    total_tokens = 0
    total_for_log = original_total if original_total is not None else len(results)

    for row in results:
        row_text = json.dumps(row, default=str)
        row_tokens = count_tokens(row_text)

        if total_tokens + row_tokens > max_tokens and kept:
            logger.warning(
                ls.QUERY_RESULTS_TRUNCATED.format(
                    kept=len(kept),
                    total=total_for_log,
                    tokens=total_tokens,
                    max_tokens=max_tokens,
                )
            )
            return kept, total_tokens, True

        kept.append(row)
        total_tokens += row_tokens

    return kept, total_tokens, False


def estimate_message_tokens(messages: Iterable[object]) -> int:
    """A provider-independent estimate of what a message history costs.

    Exists because the exact count is only available from Anthropic, and a
    counter that runs for one provider is a counter that silently does not
    bound anyone else's context (#1500). This is deliberately the same
    tiktoken measure the rest of the codebase already budgets in, so the
    pruning floor and this trigger speak one unit.

    Content that is not a string is stringified rather than skipped. Skipping
    would under-report exactly the structured tool payloads that dominate a
    long session, and under-reporting is the direction that fails to compact.
    """
    total = 0
    for message in messages:
        for part in getattr(message, "parts", ()) or ():
            content = getattr(part, "content", None)
            if content is None:
                continue
            total += count_tokens(content if isinstance(content, str) else str(content))
    return total
