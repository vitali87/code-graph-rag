from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from ... import logs as ls

if TYPE_CHECKING:
    from .dialect import GraphDialect

# Deliberately small: an isolated probe against a live server showed that
# an ArcadeDB UNWIND batch where 2+ rows MERGE onto the same vertex (e.g.
# many files IMPORTS-ing the same popular module) deadlocks deterministically
# -- retrying the identical query mostly reproduces the identical internal
# race, so a large budget here just delays the real fix. ArcadeDBIngestor's
# _execute_batch falls back to one MERGE per row (immune to this, per the
# same probe: 300/300 succeeded one row at a time) once this is exhausted,
# so this only needs to cover genuine short-lived contention on the way to
# that fallback, not the deadlock itself.
DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY_S = 0.05


def retry_on_transient[T](
    fn: Callable[[], T],
    dialect: GraphDialect,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
) -> T:
    """Run `fn`, retrying only errors the dialect calls transient.

    Memgraph's dialect never does, so this is a straight pass-through on
    the default backend. ArcadeDB is MVCC/optimistic and raises on
    concurrent updates to the same vertex, which parallel flush provokes
    whenever many edges converge on one hot node.
    """
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not dialect.is_retryable(exc):
                raise
            last = exc
            if attempt == attempts - 1:
                break
            delay = base_delay * (2**attempt) * (0.5 + random.random())  # noqa: S311
            logger.debug(
                ls.GRAPH_RETRY_TRANSIENT.format(
                    attempt=attempt + 1, attempts=attempts, delay=delay, error=exc
                )
            )
            time.sleep(delay)
    assert last is not None
    raise last
