from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from ... import logs as ls

if TYPE_CHECKING:
    from .dialect import GraphDialect

# An isolated probe against a live server showed that an ArcadeDB UNWIND
# batch where 2+ *rows in the same call* MERGE onto the same vertex (e.g.
# many files IMPORTS-ing the same popular module) can deadlock
# deterministically, or worse, silently drop the colliding row -- retrying
# the identical query just reproduces the identical internal race, so no
# retry budget fixes that class of failure. ArcadeDBIngestor's
# _chunk_endpoint_disjoint (arcadedb.py) is what actually prevents it, by
# never sending such a batch in the first place.
#
# What this budget is for instead is the failure that remains once
# collisions can no longer happen within one call: genuine external MVCC
# contention, when two *different* relationship-pattern groups flush
# concurrently (flush_relationships fans out across an executor, like
# flush_nodes) and both happen to touch the same shared vertex at once.
# That is a normal, short-lived optimistic-concurrency conflict -- exactly
# what retrying is for -- so a small budget clears it without masking a
# real deadlock behind a long one.
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
