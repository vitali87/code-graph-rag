"""The incomplete-run marker's own policy, driven through `RunMarker`.

Each test builds a `RunMarker` over the dict-backed `marker_store` fake and
reaches its state through the marker's public methods: `invalidate` raises
the in-process flag for damage no marker records, a clear the store refuses
strands a marker (`require_cleared`, `abandon_before_writing`), and a second
`RunMarker` over the same store is another process. What each MCP tool does
with the marker is tested through the tools, in `test_mcp_project_scoping.py`
and `test_mcp_update_and_search.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.mcp.run_marker import RunMarker
from codebase_rag.tests.conftest import marker_store

ALPHA = "alpha__aaaa1111"
BETA = "beta__bbbb2222"


def _marker(store: MagicMock) -> RunMarker:
    """A marker writing through `store`; each one is a separate process."""
    return RunMarker(lambda: store)


@contextmanager
def _store_refuses(store: MagicMock, what: str) -> Iterator[None]:
    """Inside the block the store refuses `what`: "mark", "clear" or "read"."""
    store._failing.add(what)
    try:
        yield
    finally:
        store._failing.discard(what)


def _strand(marker: RunMarker, store: MagicMock, project: str) -> None:
    """A read-only run whose own clear the store refused.

    Leaves a `writing=false` marker for `project` in the store and, from a
    clean flag, the flag attributed to `project` with the licence to heal it
    once that marker is recovered.
    """
    assert marker.require(project, writing=False) is None
    with _store_refuses(store, "clear"):
        assert marker.require_cleared(project) is not None, (
            "fixture guard: the clear was expected to fail"
        )


def test_an_unreadable_store_refuses_rather_than_reporting_clear() -> None:
    """ "I cannot tell" and "nothing outstanding" are different answers.

    Both the first marker read and the post-recovery re-check treat an
    unreadable store as "refuse": acting on a graph whose state is
    unknown is what the guard exists to prevent, and a full update
    recovers either way.
    """
    store = marker_store()
    marker = _marker(store)

    with _store_refuses(store, "read"):
        assert marker.persisted_incomplete(ALPHA) is True, (
            "an unreadable marker store was reported as nothing outstanding"
        )
        assert marker.rows(ALPHA) is None, (
            "an unreadable store must be distinguishable from an empty one"
        )


def test_a_clean_clear_keeps_another_projects_licence_to_heal() -> None:
    """The mirror of the attribution rule, and it fails CLOSED (#1846).

    `damage.from_failed_clear` is a project's licence to heal itself: A's
    failed clear strands a recoverable `writing=false` marker and records
    that the flag is A's to lift once the marker recovers. Dropping the
    attribution unconditionally on any successful clear meant B's clean
    clear discarded it, and A's own later reingest could no longer
    recover -- refused forever though its graph was untouched.

    Harmless compared with the other direction (a wrongly ALLOWED
    reingest), but it wedges a project with nothing wrong with it, and
    both directions come from the same unscoped assignment.
    """
    store = marker_store()
    marker = _marker(store)

    # A's clear fails, leaving a recoverable marker attributed to A.
    _strand(marker, store, ALPHA)
    assert marker.damage.from_failed_clear == ALPHA, (
        "fixture guard: A's failed clear must attribute the flag to A"
    )

    # B completes and clears its own marker.
    assert marker.require(BETA, writing=False) is None
    assert marker.require_cleared(BETA) is None

    assert marker.damage.from_failed_clear == ALPHA, (
        "B's clean clear discarded A's licence to heal itself; A's own "
        "reingest would be refused though its graph was never touched"
    )

    # And A can still recover: its marker is `writing=false`.
    assert marker.recover_then_persisted_incomplete(ALPHA) is False
    assert marker.damage.incomplete is False, (
        "A could not heal its own recoverable marker"
    )


def test_a_concurrent_runs_clear_cannot_remove_another_runs_marker() -> None:
    """The overlap race (#1709), found by CodeRabbit on #1705.

    `_ingestor_lock` is per INSTANCE, so two registries can index the same
    project at once. With one marker node per project they interleaved:

        1. A marks the project and starts mutating
        2. B marks it -- a no-op, the node already exists
        3. B finishes and CLEARS the marker
        4. A fails part way

    The graph then held A's partial data with no marker, and a fresh
    process treated it as complete. Markers are now keyed per run, so B's
    clear removes only B's.
    """
    store = marker_store()
    first, second = _marker(store), _marker(store)
    assert first.run_id != second.run_id, (
        "fixture guard: two registries must have distinct run ids, or "
        "this test cannot tell per-run markers from per-project ones"
    )

    assert first.require(ALPHA) is None
    assert second.require(ALPHA) is None
    assert second.require_cleared(ALPHA) is None

    assert first.persisted_incomplete(ALPHA) is True, (
        "the concurrent run's clear removed this run's marker; A's "
        "partial graph would look complete to a fresh process"
    )

    # A fresh process must still refuse, which is the consequence that
    # matters: the marker exists precisely to survive one.
    assert _marker(store).persisted_incomplete(ALPHA) is True, (
        "a fresh process read the project as complete over A's partial graph"
    )


def test_a_runs_own_clear_still_lifts_its_own_marker() -> None:
    """The control.

    Making a clear run-scoped must not stop a run clearing what it wrote,
    or every completed run would leave its marker behind and block the
    next one -- a fix that satisfies the overlap test while wedging the
    ordinary path.
    """
    marker = _marker(marker_store())

    assert marker.require(ALPHA) is None
    assert marker.persisted_incomplete(ALPHA) is True
    assert marker.require_cleared(ALPHA) is None
    assert marker.persisted_incomplete(ALPHA) is False, (
        "a run could not clear the marker it wrote itself"
    )


def test_a_legacy_marker_is_cleared_by_a_completing_run() -> None:
    """Markers written before run ids must not wedge the project (#1850).

    A marker from before this change has no `run_id` property. The read
    matches on project alone and still sees it; the run-scoped clear
    matches on `run_id` and can never delete it. Recovery reaches only
    `writing=false` markers, so a legacy marker with `writing=true` (or
    absent, which coalesces to true) was unreachable by every path and
    the project stayed blocked forever after an upgrade.
    """
    store = marker_store()
    marker = _marker(store)

    # A legacy marker: modelled as the sentinel empty run id. Left in the
    # READ-ONLY phase, which is the shape a stale pre-upgrade marker has
    # once its run is gone -- a `writing=true` legacy marker belongs to an
    # old-version process that may still be mid-write, and the phase guard
    # deliberately leaves that one alone (see the sibling test below).
    store.execute_write(
        cq.CYPHER_MARK_PROJECT_INCOMPLETE,
        {cs.KEY_PROJECT_NAME: ALPHA, cs.KEY_RUN_ID: "", cs.KEY_WRITING: False},
    )
    assert store._marker_store.get(ALPHA) is True, (
        "fixture guard: the legacy marker must be in the store"
    )
    # Deliberately NOT read through `persisted_incomplete` here: that call
    # RECOVERS a read-only marker as a side effect, so it would clear the
    # very thing under test before the clear below runs.

    # A run completes for this project: its clear lifts its own marker
    # and the legacy one with it.
    assert marker.require(ALPHA) is None
    assert marker.require_cleared(ALPHA) is None

    assert marker.persisted_incomplete(ALPHA) is False, (
        "a legacy marker survived a completed run, so the project stays "
        "blocked with no path that can clear it"
    )


def test_a_writing_legacy_marker_is_left_alone() -> None:
    """The rolling-deployment case (#1850 review).

    During an upgrade an old-version process can still be WRITING under a
    legacy marker. A new-version run completing for the same project must
    not delete that protection, or a later failure in the old process
    leaves partial data with nothing recording it.

    The pair matters: the sibling test requires a stale read-only legacy
    marker to be cleared (or an upgraded project is blocked forever), and
    this one requires a writing legacy marker to survive. A fix
    satisfying only one is the bug in the other direction.
    """
    store = marker_store()
    marker = _marker(store)

    # An old-version process, mid-write, under a legacy marker.
    store.execute_write(
        cq.CYPHER_MARK_PROJECT_INCOMPLETE,
        {cs.KEY_PROJECT_NAME: ALPHA, cs.KEY_RUN_ID: "", cs.KEY_WRITING: True},
    )
    assert marker.persisted_incomplete(ALPHA) is True

    # A new-version run completes for the same project.
    assert marker.require(ALPHA) is None
    assert marker.require_cleared(ALPHA) is None

    assert marker.persisted_incomplete(ALPHA) is True, (
        "a completing new-version run deleted the protection for an "
        "old-version process that was still writing; a later failure "
        "there would leave partial data with nothing recording it"
    )


def test_a_failed_legacy_cleanup_does_not_fail_a_completed_run() -> None:
    """The legacy cleanup is best effort (#1850 review).

    This run's own clear has already succeeded by the time it runs, so
    the run IS complete. Letting a failure in optional compatibility
    cleanup propagate reported a completed operation as incomplete and
    raised the flag for a graph that is whole. The legacy marker simply
    waits for the next run.
    """
    store = marker_store()
    marker = _marker(store)
    assert marker.require(ALPHA) is None

    # Fail only the LEGACY delete, leaving this run's own clear working.
    real_write = store.execute_write.side_effect

    def _fail_legacy(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_CLEAR_LEGACY_PROJECT_INCOMPLETE:
            raise RuntimeError("legacy cleanup refused")
        return real_write(query, params)

    store.execute_write.side_effect = _fail_legacy
    stuck = marker.require_cleared(ALPHA)
    store.execute_write.side_effect = real_write

    assert stuck is None, (
        "a failure in optional legacy cleanup reported a COMPLETED run as "
        f"incomplete: {stuck}"
    )
    assert marker.damage.incomplete is False, (
        "the flag was raised for a graph that is whole"
    )


def test_recovery_clears_a_marker_stranded_by_another_run() -> None:
    """Recovery is deliberately NOT run-scoped, and that is load-bearing.

    Its whole purpose is to clear a marker some OTHER run stranded -- one
    that stopped before its first graph write and could not clear its own.
    A run-scoped delete matches nothing there, and the project stays
    blocked until someone runs a full update. Three existing tests cover
    the paths; this states the property directly so the reason survives.
    """
    store = marker_store()
    stranding = _marker(store)

    # A read-only run marks itself, then fails to clear.
    _strand(stranding, store, ALPHA)
    assert store._marker_store.get(ALPHA) is True

    # A DIFFERENT process, with a different run id, recovers it.
    fresh = _marker(store)
    assert fresh.run_id != stranding.run_id, "fixture guard: distinct runs"
    assert fresh.persisted_incomplete(ALPHA) is False, (
        "a fresh process could not clear a `writing=false` marker another "
        "run stranded, so the project stays blocked until a full update"
    )


def test_recovery_deletes_only_the_read_only_markers() -> None:
    """Recovery re-checks the phase AT DELETE TIME (#1850 review).

    `persisted_incomplete` reads the phase and then deletes, and a
    concurrent run can promote its own marker in between -- an
    unconditional delete would remove a marker protecting a graph that IS
    being written. The re-check makes the delete act on the rows as they
    are now, not as the caller last saw them.

    Reaching it needs a marker set where the read still proceeds (the
    first row it sees is read-only) while another marker is writing, so
    the phase must be per MARKER. It is: the store models it that way
    precisely so this case is expressible.
    """
    store = marker_store()
    stranding, writer = _marker(store), _marker(store)

    # One read-only marker (recoverable) and one writing marker.
    assert stranding.require(ALPHA, writing=False) is None
    assert writer.require(ALPHA, writing=True) is None
    assert store._writing_by_marker[(ALPHA, stranding.run_id)] is False
    assert store._writing_by_marker[(ALPHA, writer.run_id)] is True

    # Recovery runs. It may clear the read-only marker; it must NOT clear
    # the writing one, so the project still reads incomplete.
    _marker(store).recover_stranded(ALPHA)

    assert (ALPHA, writer.run_id) in store._writing_by_marker or (
        writer.run_id in store._marker_runs.get(ALPHA, set())
    ), "recovery deleted a marker belonging to a run that was WRITING"
    assert store._marker_store.get(ALPHA) is True, (
        "recovery cleared the project though a writing run is outstanding; "
        "a scoped reingest would trust a graph being written"
    )


def test_recovery_rechecks_before_reporting_the_project_clear() -> None:
    """A conditional delete plus a write with no row count (#1850 review).

    Recovery deletes only markers still read-only, and `execute_write`
    reports no affected-row count, so "the write succeeded" does not mean
    "the marker is gone". A run that promotes its own marker between this
    method's READ and that DELETE leaves a row the delete skipped, and
    reporting the project clear then lets a scoped reingest proceed over a
    graph being written.

    The interleaving is forced rather than hoped for: the concurrent mark
    is issued by the store as the recovery's delete reaches it, which is
    the only way to land it in that window deterministically.
    """
    store = marker_store()
    stranding, writer, fresh = _marker(store), _marker(store), _marker(store)

    # Only a read-only marker exists when the read happens.
    assert stranding.require(ALPHA, writing=False) is None

    real_write = store.execute_write.side_effect

    def _racing(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_RECOVER_PROJECT_INCOMPLETE:
            # The concurrent writer promotes its marker inside the window.
            store.execute_write.side_effect = real_write
            writer.require(ALPHA, writing=True)
        return real_write(query, params)

    store.execute_write.side_effect = _racing
    verdict = fresh.persisted_incomplete(ALPHA)
    store.execute_write.side_effect = real_write

    assert store._marker_store.get(ALPHA) is True, (
        "fixture guard: the writing marker must survive the conditional "
        "delete, or the race under test did not occur"
    )
    assert verdict is True, (
        "the project was reported clear while a writing marker remained; "
        "a scoped reingest would hydrate from a graph being written"
    )


def test_recovery_leaves_a_marker_that_began_writing() -> None:
    """A promoted phase must stop the marker being recovered away.

    Covers the OUTER property: a project whose marker has been promoted
    to writing still reads incomplete to a fresh process. It stops at the
    phase READ in `persisted_incomplete`, which returns before recovery
    is called, so it does not reach the re-check inside
    `CYPHER_RECOVER_PROJECT_INCOMPLETE`.

    That re-check is covered by
    `test_recovery_deletes_only_the_read_only_markers`, which needs a
    marker set where the read still proceeds while another marker is
    writing -- expressible only because the store models the phase per
    MARKER rather than per project.
    """
    store = marker_store()
    stranding, writer = _marker(store), _marker(store)

    # A read-only run strands its marker.
    _strand(stranding, store, ALPHA)

    # A concurrent run promotes the phase to writing before recovery acts.
    assert writer.require_writing(ALPHA) is None
    assert store._writing_store.get(ALPHA) is True, (
        "fixture guard: the phase must be promoted, or recovery is "
        "entitled to clear and this test proves nothing"
    )

    assert _marker(store).persisted_incomplete(ALPHA) is True, (
        "recovery cleared a marker belonging to a run that had begun "
        "writing; a scoped reingest would trust a partial graph"
    )


def test_a_failed_recovery_write_refuses() -> None:
    """If the store refuses the recovery write, the run that would follow
    could not mark itself either, so refuse now and let the next attempt
    retry."""
    store = marker_store()
    _strand(_marker(store), store, ALPHA)
    fresh = _marker(store)

    real_write = store.execute_write.side_effect

    def _fail_recovery(query: str, params: dict | None = None) -> None:
        if query == cq.CYPHER_RECOVER_PROJECT_INCOMPLETE:
            raise RuntimeError("recovery write refused")
        return real_write(query, params)

    store.execute_write.side_effect = _fail_recovery
    assert fresh.recover_stranded(ALPHA) is False
    verdict = fresh.persisted_incomplete(ALPHA)
    store.execute_write.side_effect = real_write

    assert verdict is True, (
        "a store that refused the recovery write was reported as clear"
    )


def test_a_no_write_mark_cannot_demote_another_runs_writing_marker() -> None:
    """The phase is monotonic across processes.

    Process A marks writing and starts changing the graph; process B's
    retained reingest marks the same project `writing=false` and aborts
    before writing. B's mark must not lower A's phase: a fresh process C
    reading `writing=false` would clear the marker and hydrate from A's
    partial graph (#1705 review). Whether B's CLEAR may delete A's marker
    at all is the ownership question tracked in #1709; this pins the half
    that a phase-monotonic mark closes.
    """
    store = marker_store()
    a = _marker(store)
    assert a.require(ALPHA, writing=True) is None
    assert store._writing_store.get(ALPHA) is True

    b = _marker(store)
    assert b.require(ALPHA, writing=False) is None
    assert store._writing_store.get(ALPHA) is True, (
        "a writing=false mark demoted a marker another run left at writing"
    )

    assert _marker(store).persisted_incomplete(ALPHA) is True, (
        "a fresh process cleared a marker over a graph another run is still changing"
    )


def test_the_mark_query_never_lowers_the_phase() -> None:
    """Pins the production Cypher the fake store models.

    The fake's monotonic phase is only faithful while the MERGE reads the
    existing value back; a `SET m.writing = $writing` would demote another
    run's marker and the fake would not notice. The query text is the one
    place that can be asserted without a live store.
    """
    assert "coalesce(m.writing, false) OR $writing" in (
        cq.CYPHER_MARK_PROJECT_INCOMPLETE
    )
    assert "m.writing = $writing" not in cq.CYPHER_MARK_PROJECT_INCOMPLETE
