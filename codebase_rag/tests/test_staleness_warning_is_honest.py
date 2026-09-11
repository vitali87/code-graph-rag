"""The staleness warning must not promise a rebuild it does not perform.

Issue #1649. The warning told users to delete three cache files and re-index,
promising "every file is then treated as new". That re-parse does not
reconcile EDGES: `CYPHER_DELETE_MODULE` removes the module and what it
DEFINES, so an edge whose other end is an inferred or external node the
re-parse never touches outlives the remedy.

Measured on gin-gonic/gin across the #1641 fix: 82 `INSTANTIATES` edges before,
12 after the documented remedy, 0 in a fresh copy of identical source. All 12
survivors were stale.

The partial result is the dangerous part -- 70 of 82 disappearing looks
exactly like the remedy working, and the user gets no signal that 12 remain.
So the text has to say what the remedy actually achieves.

These assert PROPERTIES of the message rather than its exact wording: the
text may be reworded, but it must not go back to promising a complete
rebuild, and it must keep naming the edge case and a route that works.
"""

from __future__ import annotations

from codebase_rag import logs

_WARNING = logs.PARSER_FINGERPRINT_MISMATCH


def test_it_does_not_promise_that_every_file_is_new() -> None:
    """The specific phrase that made the partial result misleading.

    "every file is then treated as new" is confident and specific about what
    the cache deletion achieves, and it is what stops a reader looking
    further. Every file IS re-parsed; the claim that fails is the one a
    reader takes from it, that the result equals a fresh index.
    """
    assert "treated as new" not in _WARNING


def test_it_says_edges_can_survive_the_remedy() -> None:
    """Node survivors were already covered; edges were the unaccounted case."""
    assert "edges" in _WARNING.lower()


def test_it_says_what_a_reparse_actually_removes() -> None:
    """The mechanism, so the limit is checkable rather than asserted.

    A re-parse removes what the re-parsed module DEFINES. That is why an edge
    to an inferred or external node outlives it, and naming the mechanism is
    what lets a reader predict which of their own edges are at risk.
    """
    assert "DEFINES" in _WARNING


def test_it_offers_a_route_that_actually_rebuilds() -> None:
    """Naming the limit without a way out would leave the user stuck.

    Indexing as a new project needs no deletion at all, which matters because
    the obvious alternative is destructive.
    """
    assert "NEW" in _WARNING or "new project" in _WARNING


def test_it_still_warns_that_clean_deletes_every_project() -> None:
    """The pre-existing caveat must survive the rewrite.

    `--clean` deletes every project in a shared database, so a user following
    this advice on a shared store loses unrelated work. A rewrite that
    dropped this to make room for the edge caveat would trade one hazard for
    a worse one.
    """
    assert "--clean" in _WARNING
    lowered = _WARNING.lower()
    assert "every project" in lowered
    assert "shared database" in lowered


def test_it_still_names_the_three_cache_files() -> None:
    """The remedy is still the right first step for the common case; this is
    a correction to what it claims, not a removal of the advice."""
    for filename in (
        ".cgr-hash-cache.json",
        ".cgr-dir-mtimes.json",
        ".cgr-parser-fingerprint",
    ):
        assert filename in _WARNING
