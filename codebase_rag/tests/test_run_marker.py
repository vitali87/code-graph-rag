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
GAMMA = "gamma__cccc3333"


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


def _refused(marker: RunMarker, project: str) -> bool:
    return marker.partial_graph_refusal(project, cs.MCPToolName.ASK_AGENT) is not None


# A store OUTAGE is not an incomplete graph.
#
# `persisted_incomplete` answers True when it cannot reach the store, which is
# right for a reingest (never start on an unknown state) and wrong for a read:
# it reported every outage as "graph incomplete" and buried the real error.
# Reads go through `says_incomplete`, which treats an unreachable store as no
# verdict. `test_mcp_errors_are_reported_not_raised` in test_graph_query.py
# pins this for the `_graph_query` dispatcher; seven readers bypass that
# dispatcher, so the covered path is not the broken one (M1 CGR-3, PR #1547).
def test_a_dead_store_is_not_reported_as_an_incomplete_graph() -> None:
    store = marker_store()
    marker = _marker(store)

    with _store_refuses(store, "read"):
        # The decision the seven share. False means "no verdict, carry on and
        # let the read fail as itself" -- NOT "the graph is fine".
        assert marker.says_incomplete(ALPHA) is False
        assert (
            marker.partial_graph_refusal(ALPHA, cs.MCPToolName.GET_FUNCTION_SOURCE)
            is None
        )


def test_the_reingest_default_still_refuses_on_a_dead_store() -> None:
    # The asymmetry is the point: same store, opposite answer, because a
    # write that starts on an unknown graph cannot be undone.
    store = marker_store()

    with _store_refuses(store, "read"):
        assert _marker(store).persisted_incomplete(ALPHA) is True


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


# One project's damage must not make every other project unreadable, and a
# healthy project's recovery must not clear the damaged one's warning
# (Greptile, PR #1547).
def test_one_projects_damage_does_not_block_another() -> None:
    marker = _marker(marker_store())

    marker.invalidate(ALPHA)

    assert _refused(marker, ALPHA)
    assert not _refused(marker, BETA)


# A successful clear for ONE project must not speak for another, and a
# project-scoped latch must not block an unrelated project's recovery
# (Greptile, PR #1547).
def test_clearing_one_project_keeps_anothers_warning() -> None:
    """ALPHA is partial in-process only; clearing healthy BETA must not heal it.

    The damaging failure could not write ALPHA's durable marker, so the
    in-process flag is the ONLY record that ALPHA is partial. A blanket
    reset on BETA's success discards it and lets reads onto ALPHA's
    partial graph.
    """
    marker = _marker(marker_store())
    marker.invalidate(ALPHA)

    assert marker.require_cleared(BETA) is None

    assert marker.damage.incomplete is True, "BETA's clear healed ALPHA's flag"
    assert marker.damage.project == ALPHA
    assert _refused(marker, ALPHA)


def test_clearing_a_project_still_heals_its_own_warning() -> None:
    """The control: a clear for the project the flag is ABOUT must heal it.

    Without this, scoping the reset could be implemented as "never reset",
    which passes the test above and breaks recovery entirely.
    """
    marker = _marker(marker_store())
    marker.invalidate(ALPHA)

    assert marker.require_cleared(ALPHA) is None

    assert marker.damage.incomplete is False
    assert marker.damage.project is None
    assert not _refused(marker, ALPHA)


def test_clearing_a_project_still_heals_unattributed_damage() -> None:
    """An unattributed flag is owned by nobody, so any clear may settle it.

    This is the pre-existing behaviour for the common single-project case,
    where the flag is set with no name and cleared by the run that earned it.
    """
    marker = _marker(marker_store())
    # Staged: only a wipe in progress holds an ownerless flag; no method sets one.
    marker.damage.incomplete = True

    assert marker.require_cleared(ALPHA) is None

    assert marker.damage.incomplete is False
    assert marker.damage.project is None


def test_two_damaged_projects_block_either_projects_clear() -> None:
    """The two-named-projects route into the broad unattributed state.

    `invalidate` widens the attribution to None when a SECOND, different
    project is damaged. That None means "more than one project is partial",
    and neither project's successful clear may retire it.

    Distinct from the failed-wipe route asserted in
    `test_a_successful_clear_cannot_settle_a_widened_flag`: both end at
    `damage.project is None`, and mutating this widening was invisible
    to every other test in the suite. Both directions are checked below,
    because a guard reading the flag for ALPHA and not for BETA would be
    satisfied by testing only one.
    """
    for clearing in (ALPHA, BETA):
        marker = _marker(marker_store())
        marker.invalidate(ALPHA)
        marker.invalidate(BETA)

        assert marker.damage.project is None, (
            "fixture guard: two different damaged projects must widen the "
            "attribution to None, or the clear takes the attributed branch "
            "and this proves nothing"
        )

        assert marker.require_cleared(clearing) is None

        assert marker.damage.incomplete is True, (
            f"{clearing} cleared its own marker and settled a flag that "
            "records damage to two projects"
        )


def test_repairing_every_damaged_project_lifts_the_refusal() -> None:
    """The mirror-image bug: a refusal with no way out.

    The first version of this guard was a BOOLEAN saying "the damage spans
    projects". Nothing could ever lower it -- no sequence of successful
    repairs retires a flag that counts nothing -- so once two projects were
    damaged, every project was refused for the life of the process, including
    projects never damaged at all, with `wipe_database` the only escape.

    That is worse than the leak it fixed: a leak lets a stale read through,
    this makes the server useless until restarted. Recording WHICH projects
    are outstanding is what makes the flag retirable.

    Asserted through the refusal rather than the field, and on a project that
    was never damaged as well as the two that were: an over-broad latch
    refuses GAMMA too, and only a behavioural assertion notices.
    """
    marker = _marker(marker_store())
    marker.invalidate(ALPHA)
    marker.invalidate(BETA)

    # One assertion per project: a composite reports "some project did not
    # refuse" and leaves the reader to find which, and WHICH is the whole
    # diagnosis here -- a damaged project not refusing is a different bug
    # from an undamaged one not refusing (SonarCloud S9073).
    assert _refused(marker, ALPHA), (
        "fixture guard: a damaged project must refuse before the repair below"
    )
    assert _refused(marker, BETA), (
        "fixture guard: the second damaged project must refuse too"
    )
    assert _refused(marker, GAMMA), (
        "fixture guard: an unattributed flag must block a project it does not "
        "name, or the widening is not in the broad state this test needs"
    )

    marker.require_cleared(ALPHA)
    assert _refused(marker, BETA), (
        "repairing one of two damaged projects lifted the other's refusal"
    )

    marker.require_cleared(BETA)

    assert not _refused(marker, ALPHA), (
        "the first damaged project was repaired and its reads are still "
        "refused; nothing short of a full wipe can clear this"
    )
    assert not _refused(marker, BETA), (
        "the last damaged project was repaired and its reads are still "
        "refused, so the outstanding set never empties"
    )
    assert not _refused(marker, GAMMA), (
        "a project that was never damaged is still refused after every "
        "damaged project was repaired"
    )


def test_a_third_damaged_project_is_recorded_like_the_first_two() -> None:
    """The route into the broad state that recorded nothing.

    `invalidate` notes BOTH names when the attribution widens, but a THIRD
    project damaged afterwards takes the "already unattributed, keep it"
    branch -- correct about the attribution, and silent about the fact
    that another project is now outstanding.

    So A, B and C damaged, then A and B repaired, and the set empties while C
    is still partial: C's reads reopen onto a graph nothing ever finished
    (Greptile, PR #1547). Two damaged projects was the case I tested and
    three is the case that was broken, which is the whole lesson.
    """
    marker = _marker(marker_store())
    for project in (ALPHA, BETA, GAMMA):
        marker.invalidate(project)

    assert _refused(marker, GAMMA), (
        "fixture guard: three damaged projects must all refuse"
    )

    marker.require_cleared(ALPHA)
    marker.require_cleared(BETA)

    assert _refused(marker, GAMMA), (
        "repairing two of three damaged projects settled the flag, so the "
        "third project's reads reopened while it is still partial"
    )

    marker.require_cleared(GAMMA)
    assert not _refused(marker, GAMMA), (
        "the last outstanding project was repaired and the flag still stands"
    )


def test_a_recovered_marker_cannot_retire_a_flag_spanning_projects() -> None:
    """`recover_then_persisted_incomplete`, the heal the reingest and rename
    guards share, on the same trap.

    A stranded marker that this project later recovers lifts the flag its
    own failed clear raised. But if a SECOND project was damaged in between,
    the flag no longer describes only this project's stranded marker, and
    recovering the marker is evidence about one project rather than two.

    The route into the state matters: the flag must be spanning at the
    moment of recovery, with `damage.from_failed_clear` still naming this
    project, or the guard is never reached and the test is vacuous. BETA's
    damage is therefore an abandoned no-write run whose clear also failed:
    that widens the attribution and, unlike `invalidate`, leaves ALPHA's
    licence in place.
    """
    store = marker_store()
    marker = _marker(store)

    # ALPHA's clear fails, stranding a marker it is licensed to recover.
    _strand(marker, store, ALPHA)
    assert marker.damage.from_failed_clear == ALPHA, (
        "fixture guard: ALPHA must hold the recovery licence, or the guard "
        "under test is never reached"
    )

    # BETA is damaged too, so the flag now spans both.
    assert marker.require(BETA, writing=False) is None
    with _store_refuses(store, "clear"):
        marker.abandon_before_writing(BETA)
    assert marker.damage.projects == {ALPHA, BETA}, (
        "fixture guard: both projects must be outstanding before the recovery"
    )
    assert marker.damage.from_failed_clear == ALPHA, (
        "fixture guard: ALPHA must still hold the licence when it recovers"
    )

    # ALPHA's stranded marker says `writing=false`, so the recovery clears it.
    assert marker.recover_then_persisted_incomplete(ALPHA) is False, (
        "fixture guard: ALPHA's marker must recover, or the heal is never weighed"
    )

    assert marker.damage.incomplete is True, (
        "ALPHA recovering its own stranded marker retired a flag that also "
        "records BETA's damage"
    )
    # The refusal is the wanted outcome, and it is stronger evidence than a
    # state read: ALPHA's marker is gone, so only the flag can refuse.
    assert _refused(marker, ALPHA)


def test_a_rollback_widening_is_recorded_like_any_other() -> None:
    """`abandon_before_writing` widens to None too, and must say so.

    It reaches the broad unattributed state by the same reasoning as
    `invalidate` -- an owner already in place naming a different project
    cannot be replaced, so the pair becomes None. Recording it there and not
    here would leave one route into the state indistinguishable from a
    never-attributed flag, which is the original bug with a different entry
    point.
    """
    store = marker_store()
    marker = _marker(store)
    marker.invalidate(ALPHA)
    assert marker.require(BETA, writing=False) is None

    with _store_refuses(store, "clear"):
        marker.abandon_before_writing(BETA)

    assert marker.damage.project is None, (
        "fixture guard: an abandon naming a different project, over a flag "
        "already owned by ALPHA, must widen the attribution to None"
    )
    assert marker.damage.projects == {ALPHA, BETA}, (
        "the rollback widened the attribution without recording WHICH "
        "projects are outstanding, so the next successful clear settles it"
    )


def test_a_failed_clear_from_clean_attributes_the_flag_to_itself() -> None:
    """A stuck marker on an otherwise clean registry is this project's to own.

    Nothing else has claimed the flag, so the refusal and the
    `damage.from_failed_clear` recovery are both about the project whose
    marker is actually stuck.
    """
    store = marker_store()
    marker = _marker(store)

    with _store_refuses(store, "clear"):
        assert marker.require_cleared(BETA) is not None

    assert marker.damage.incomplete is True
    assert marker.damage.project == BETA
    assert marker.damage.from_failed_clear == BETA


def test_a_failed_clear_does_not_steal_an_earlier_owners_flag() -> None:
    """A flag already up records damage this run did not cause.

    Claiming it would let BETA's recoverable marker lift the refusal ALPHA
    earned: `recover_then_persisted_incomplete` heals a flag whose
    `damage.from_failed_clear` names the project being re-ingested, so
    attributing ALPHA's flag to BETA hands BETA the licence to clear it.
    `abandon_before_writing` already takes exactly this care.
    """
    store = marker_store()
    marker = _marker(store)
    marker.invalidate(ALPHA)

    with _store_refuses(store, "clear"):
        assert marker.require_cleared(BETA) is not None

    assert marker.damage.incomplete is True
    assert marker.damage.project == ALPHA, "BETA stole ALPHA's attribution"
    assert marker.damage.from_failed_clear is None, "BETA gained a licence to heal"


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


# The attribution field holds ONE project, so it can only ever narrow to a
# name when nothing broader already owns the flag (Greptile, PR #1547).
def test_a_second_damaged_project_widens_to_unattributed() -> None:
    """Two damaged projects cannot both be named in a one-project field.

    Keeping ALPHA would declare BETA healthy and replacing it would declare
    ALPHA healthy, so the only safe answer is that neither is.
    """
    marker = _marker(marker_store())
    marker.invalidate(ALPHA)

    marker.invalidate(BETA)

    assert marker.damage.incomplete is True
    assert marker.damage.project is None


def test_a_first_failure_still_attributes_to_its_project() -> None:
    """The control: with no flag up, the damage IS this project's.

    Without this, the widening rule could be implemented as "never
    attribute", which passes both tests above and makes every failure block
    every project -- the bug the scoping was added to fix.
    """
    marker = _marker(marker_store())

    marker.invalidate(ALPHA)

    assert marker.damage.incomplete is True
    assert marker.damage.project == ALPHA


def test_re_flagging_the_same_project_keeps_its_name() -> None:
    """The second control: repeated damage to ONE project must not widen.

    A project that fails twice is still the only damaged project, and
    widening would block every other project for no reason.
    """
    marker = _marker(marker_store())
    marker.invalidate(ALPHA)

    marker.invalidate(ALPHA)

    assert marker.damage.incomplete is True
    assert marker.damage.project == ALPHA


def test_widening_the_owner_drops_a_stale_healing_licence() -> None:
    """A licence naming a project the flag is no longer about must not survive.

    `recover_then_persisted_incomplete` keys `recoverable_here` off the
    licence alone, so a surviving one lets that project's reingest clear a
    flag that now covers everything. This asserts the INVARIANT over the
    state the rollback site produces through `invalidate`.
    """
    store = marker_store()
    marker = _marker(store)
    # BETA's failed clear leaves the flag BETA's, with the licence to heal it.
    _strand(marker, store, BETA)

    # The rollback site's invalidation, for a DIFFERENT project.
    marker.invalidate(ALPHA)

    assert marker.damage.project is None
    assert marker.damage.from_failed_clear is None, (
        "a licence for BETA survived a widening BETA no longer owns"
    )
    # The consequence the licence would have had: BETA's marker recovers and
    # the refusal still stands.
    assert marker.recover_then_persisted_incomplete(BETA) is False
    assert _refused(marker, BETA)


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
