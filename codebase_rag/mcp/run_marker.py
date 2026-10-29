"""The durable record that a project's last run did not finish.

Every mutating MCP path applies one incomplete-run invariant (#1705):

  (a) No destructive step starts unless the durable marker was
      persisted. A failed persist aborts BEFORE any cleanup, embedding
      purge, constraint migration or delete_project, and reports a
      retryable failure. Nothing has changed yet, so aborting is free.
  (b) Success is reported only after the marker is durably cleared. A
      failed clear after a successful operation is a retryable failure,
      never success with the marker retained -- that state blocks every
      later reingest and the user cannot see why.
  (c) The fresh scoped-reingest path is a destructive step too: its
      `ensure_constraints` runs the same migration that drops
      constraints and can purge, so it marks first like the others.

`RunMarker.require` and `RunMarker.require_cleared` are the only two places
that decide what a marker failure means; call sites choose the failure
VALUE, not the policy.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag import logs as ls
from codebase_rag import tool_errors as te
from codebase_rag.mcp.graph_damage import GraphDamage
from codebase_rag.services import QueryProtocol
from codebase_rag.types_defs import PropertyValue


class RunMarker:
    """One registry's `:IncompleteRun` marker, and the in-process damage
    record kept in step with it."""

    def __init__(self, ingestor: Callable[[], QueryProtocol]) -> None:
        # The registry's ingestor, read on each use: the marker is written
        # through whichever graph the registry serves.
        self._ingestor = ingestor
        # What this process knows about damage to the graph it serves.
        self.damage = GraphDamage()
        # This registry's identity in the durable marker. The ingestor lock is
        # per INSTANCE, so two registries can index the same project at once;
        # a marker keyed only by project meant the second run's clear deleted
        # the first run's marker, and a partial graph then looked complete to
        # a fresh process (issue #1709). Per registry rather than per call
        # because the instance lock already serialises this registry's own
        # runs, so its own marker can never overlap itself.
        self.run_id = uuid.uuid4().hex

    def persist(
        self, project_name: str, incomplete: bool, *, writing: bool = True
    ) -> bool:
        """Record (or clear) the incomplete-run marker on its own node.

        `writing` is the marker's phase (see `CYPHER_MARK_PROJECT_INCOMPLETE`):
        False while the run is still doing read-only or graph-external work,
        True from its first graph write. Ignored when clearing.

        `damage.incomplete` lives in this process, so it only ever covered
        the process that ran the failed update. A crash or an MCP restart
        left a fresh registry seeing a project that looks whole, hydrating a
        scoped updater from the partial graph and treating its missing
        definitions as authoritative (issue #1679). The marker survives that.

        Returns whether the write reached the store, and never raises itself;
        the CALLER decides what a failure means, because the two directions
        are not symmetric (#1705):

        * A failed MARK must abort before any destructive work. The marker is
          the only protection that survives a restart, so proceeding without
          it means a crash leaves a partial graph indistinguishable from a
          complete one. Nothing is lost by stopping: no state has changed.
        * A failed CLEAR must NOT fail a run that already succeeded. It
          leaves the graph marked incomplete, so the local flag follows the
          durable state and the next update retries the clear.

        Logged as well as returned, because a marker that never persists
        degrades this guard back to the old in-process behaviour and only the
        log would show it.
        """
        # Both the mark and the clear carry this registry's run id, so a
        # clear removes only the marker this registry wrote (issue #1709).
        # The READ deliberately does not: it asks whether ANY run is
        # outstanding, which is the same boolean question it always asked.
        params: dict[str, PropertyValue] = {
            cs.KEY_PROJECT_NAME: project_name,
            cs.KEY_RUN_ID: self.run_id,
        }
        if incomplete:
            query = cq.CYPHER_MARK_PROJECT_INCOMPLETE
            params[cs.KEY_WRITING] = writing
        else:
            query = cq.CYPHER_CLEAR_PROJECT_INCOMPLETE
        try:
            self._ingestor().execute_write(query, params)
        except Exception as error:  # noqa: BLE001 -- see docstring
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_FAILED.format(
                    project=project_name, incomplete=incomplete, error=error
                )
            )
            return False
        if not incomplete:
            # A marker written before run ids existed has no `run_id`, so the
            # run-scoped clear above cannot match it while the read still sees
            # it -- the project would stay blocked forever after an upgrade
            # (#1850). A run completing for this project has
            # established the graph is whole, so clearing the legacy marker is
            # the same statement its own clear makes.
            #
            # BEST EFFORT, and deliberately outside the try above: this run's
            # own clear has already succeeded by here, so the run IS complete.
            # Letting a failure in optional compatibility cleanup return False
            # reported a completed operation as incomplete and raised the flag
            # for a graph that is whole (#1850, second finding). The
            # legacy marker simply stays for the next run to clear.
            try:
                self._ingestor().execute_write(
                    cq.CYPHER_CLEAR_LEGACY_PROJECT_INCOMPLETE,
                    {cs.KEY_PROJECT_NAME: project_name},
                )
            except Exception as error:  # noqa: BLE001 -- best effort; see above
                logger.warning(
                    ls.MCP_INCOMPLETE_MARKER_FAILED.format(
                        project=project_name, incomplete=False, error=error
                    )
                )
        return True

    def recover_stranded(self, project_name: str) -> bool:
        """Clear EVERY outstanding marker for the project, not just this run's.

        The ordinary clear is run-scoped so a concurrent run's marker outlives
        it (issue #1709). Recovery is the opposite case by definition: it
        clears a marker some OTHER run stranded, so a run-scoped delete would
        match nothing and leave the project blocked until a full update.

        Safe only because the caller has already established that EVERY
        outstanding marker says `writing=false` -- the graph is as those runs
        found it. Returns whether the write reached the store.
        """
        try:
            self._ingestor().execute_write(
                cq.CYPHER_RECOVER_PROJECT_INCOMPLETE,
                {cs.KEY_PROJECT_NAME: project_name},
            )
        except Exception as error:  # noqa: BLE001 -- same contract as persist
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_FAILED.format(
                    project=project_name, incomplete=False, error=error
                )
            )
            return False
        return True

    def require(self, project_name: str, *, writing: bool = True) -> str | None:
        """Invariant (a): mark before destructive work, or refuse to start.

        Returns None when the marker is down, or the message to fail with.
        `writing=False` marks a run that has not yet reached a graph write;
        it must call `require_writing` before its first one.
        """
        if self.persist(project_name, True, writing=writing):
            return None
        return cs.MCP_INCOMPLETE_MARKER_REQUIRED.format(project=project_name)

    def require_writing(self, project_name: str) -> str | None:
        """Invariant (a), second half: say WRITING before the first graph write.

        A run marked `writing=False` has told a fresh process it may clear the
        marker and carry on. Once this run is about to change the graph that
        is no longer true, and it must be recorded durably BEFORE the change:
        if the record cannot be written the run stops here, with the graph
        untouched and the marker still honest. Returns None or the refusal.
        """
        return self.require(project_name, writing=True)

    def abandon_before_writing(self, project_name: str) -> None:
        """A run stopped before its first graph write: take its marker off.

        Nothing was written, so the marker is a lie about a run that changed
        nothing, and left in place it refuses every later scoped reingest
        until a full update runs. Best effort, never raising: the caller's
        own error stays primary. A clear that FAILS is logged rather than
        discarded, and leaves a `writing=False` marker that a later process
        clears for itself (`persisted_incomplete`).

        This run wrote NOTHING, so it must not touch the in-process flag or
        its attribution at all -- neither to set them nor to clear them. It
        deliberately does not go through `require_cleared`, which
        assigns both unconditionally:

        * setting them relabels damage another failure recorded. A failed
          wipe leaves `damage.incomplete=True` with NO attribution, because
          no marker records it; a later run that stops before writing then
          stamped its own project onto that flag, and the next reingest
          healed it against a recoverable marker -- discarding the wipe's
          refusal and hydrating over a half-wiped graph (#1705).
        * clearing them outright is the same bug from the other side: it
          would discard an earlier run's damage just as readily.

        So the flag is restored to whatever it was on entry.

        The captured values are read HERE rather than saved by each caller:
        a caller-side save is defeated by the next caller that forgets one,
        which leaves a stale value in the slot and restores something
        arbitrary. Capturing at entry cannot be bypassed that way.

        The scenario needs the abandoning run to name the SAME project as the
        later reingest -- that is what makes `recoverable_here` true at the
        guard. A run abandoning a DIFFERENT project reads as a near-miss and
        refuses correctly, so a test written that way passes with or without
        this fix (measured both ways).
        """
        flag_before = self.damage.incomplete
        attributed_before = self.damage.from_failed_clear
        incomplete_project_before = self.damage.project
        cleared = self.persist(project_name, False)
        # A failed clear strands a marker, so the flag must go up: this
        # process has to know later reingests will refuse. But the
        # ATTRIBUTION -- the licence to heal that flag from the marker -- is
        # only this run's to grant when the flag is this run's to explain.
        #
        # If the flag was ALREADY set on entry it records damage some earlier
        # failure found, and a wipe records damage no marker can describe.
        # Claiming it here is what let a recoverable marker lift a wipe's
        # refusal (#1705). So attribute only when this run raised the
        # flag from clean; otherwise leave the earlier owner in place, and
        # the stranded marker stays unhealable until a full update clears it.
        if not cleared:
            self.damage.incomplete = True
            # The attribution narrows the same way the flag setters do: a
            # broader owner already in place (unattributed, or a different
            # project) still describes damage this run did not cause, and
            # replacing it would tell the guards those projects are healthy.
            if not flag_before:
                self.damage.project = project_name
                self.damage.from_failed_clear = project_name
            elif incomplete_project_before not in (None, project_name):
                self.damage.project = None
                # Widened for the same reason as `invalidate`, so
                # it must be recorded the same way: BOTH names go in, because
                # both are outstanding and the flag settles when the last one
                # is repaired.
                self.damage.note_outstanding(incomplete_project_before, project_name)
        else:
            self.damage.incomplete = flag_before
            self.damage.project = incomplete_project_before
            self.damage.from_failed_clear = attributed_before
        if not cleared:
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_STUCK_AFTER_ABORT.format(project=project_name)
            )

    def invalidate(self, project_name: str) -> None:
        """Raise the incomplete flag and attribute it to `project_name`.

        The attribution only ever NARROWS to a project when nothing broader
        already holds the flag. An unattributed flag means the damage could
        not be pinned to one project -- a wipe spans them all, and a failure
        before the name was known could have touched anything -- so writing a
        project name over it would tell the reingest and read guards that
        every OTHER project is healthy, which is precisely what the
        unattributed flag denies (PR #1547). A flag already owned
        by a DIFFERENT project widens to unattributed for the same reason:
        two projects are damaged and the pair cannot be named in one field.

        `damage.from_failed_clear` is cleared unconditionally: none of these
        callers stranded a marker, so no marker recovery may heal what they
        set, whichever attribution ends up in place. The rollback site was
        briefly split out to preserve an existing licence; it turned out a
        same-project rollback then authorised the clear of its own damage,
        so it revokes like every other caller and the split is gone
        (PR #1547). Two functions that must stay in step is the
        divergence that produced that bug.
        """
        already_flagged = self.damage.incomplete
        owner = self.damage.project
        self.damage.incomplete = True
        if not already_flagged:
            self.damage.project = project_name
            self.damage.projects = set()
        elif owner is not None and owner != project_name:
            self.damage.project = None
            # Two named projects are damaged. The None written here is the
            # BROAD state, and the outstanding set records WHICH projects, so
            # a later clear cannot mistake it for the never-attributed one and
            # repairing both can still settle it.
            self.damage.note_outstanding(owner, project_name)
        elif owner is None and self.damage.projects:
            # A THIRD project damaged while the flag is already broad. It
            # keeps the unattributed attribution, but it is outstanding like
            # the other two and must be recorded or the flag settles once
            # they are repaired while this one is still partial (Greptile,
            # PR #1547): A, B and C damaged, A and B repaired, and C's reads
            # reopen onto a graph nothing finished.
            #
            # Guarded on the set being non-empty so a project damaged under an
            # UNBOUNDED flag (a failed wipe) does not turn that into a
            # settleable named set -- a wipe spans projects this process has
            # never seen, and no enumeration of repairs can retire it.
            self.damage.note_outstanding(project_name)
        # An existing flag attributed to this same project, or already
        # unattributed with nothing outstanding, keeps the attribution it has.
        self.damage.from_failed_clear = None

    def require_cleared(self, project_name: str) -> str | None:
        """Invariant (b): a run is complete only once its marker is gone.

        Also syncs the in-process flag to the durable state, so this process
        and the next agree. Returns None on success, or the failure message.
        """
        flag_before = self.damage.incomplete
        owner_before = self.damage.project
        cleared = self.persist(project_name, False)
        if not cleared:
            self.damage.incomplete = True
            # Attribute the flag to this project's stranded marker, so the
            # recovery before a scoped reingest hydrates heals only the flag it
            # explains -- but only when this run raised the flag from clean.
            # A flag already up records damage an earlier failure found, and
            # claiming it here would let this project's recoverable marker
            # lift a refusal earned elsewhere (`abandon_before_writing`
            # takes the same care for the same reason).
            if not flag_before:
                self.damage.project = project_name
                self.damage.from_failed_clear = project_name
            else:
                self.damage.project = owner_before
            return cs.MCP_INCOMPLETE_MARKER_STUCK.format(project=project_name)
        # A successful clear is evidence about THIS project only: a flag
        # attributed to a different one, or spanning several, is the sole
        # record that they are partial whenever the damaging failure could
        # not write its durable marker, so only the run that owns the flag
        # settles it (4-55, PR #1547).
        self.damage.settle(project_name)
        return None

    def partial_graph_refusal(
        self, project_name: str, tool: cs.MCPToolName
    ) -> te.ToolFailure | None:
        """The refusal for a graph read while the graph is known partial.

        Returns the message, or None to proceed. The eight tools behind
        the registry's `_graph_query` share its guard; these handlers each dispatch
        directly and have three different return shapes between them, so
        the shared part is the DECISION and the message, and each call site
        wraps it in whatever it returns (PR #1547).
        """
        in_process = self.damage.blocks(project_name)
        if in_process or self.says_incomplete(project_name):
            return te.ToolFailure(
                cs.MCP_QUERY_AFTER_FAILED_RUN.format(project=project_name, tool=tool)
            )
        return None

    def says_incomplete(self, project_name: str) -> bool:
        """`persisted_incomplete`, but an unreachable store is not a verdict.

        That method returns True when it cannot tell, so a reingest refuses
        rather than starting on an unknown graph. A READ wants the opposite
        default: a store that is down should surface as the store being down,
        not as a spurious incomplete-graph refusal that hides it.
        """
        try:
            self._ingestor().fetch_all(
                cq.CYPHER_PROJECT_IS_INCOMPLETE,
                {cs.KEY_PROJECT_NAME: project_name},
            )
        # Any failure to reach the marker means no verdict: let the read
        # itself report why the store could not be reached.
        except Exception:  # noqa: BLE001
            return False
        return self.persisted_incomplete(project_name)

    def rows(self, project_name: str) -> list[dict] | None:
        """Outstanding marker rows for the project, or None if unreadable.

        Split out so the post-recovery re-check reads the same way the first
        read does; `None` means the store could not answer, which every
        caller must treat as "refuse" rather than "nothing outstanding".
        """
        try:
            return list(
                self._ingestor().fetch_all(
                    cq.CYPHER_PROJECT_IS_INCOMPLETE,
                    {cs.KEY_PROJECT_NAME: project_name},
                )
                or []
            )
        except Exception as error:  # noqa: BLE001 -- same contract as the caller
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_UNREADABLE.format(
                    project=project_name, error=error
                )
            )
            return None

    def persisted_incomplete(self, project_name: str) -> bool:
        """Whether a previous process left this project mid-update.

        An absent property means complete, so a project indexed before this
        marker existed reads exactly like one whose run finished and no
        migration is needed. A store that cannot answer returns True: "I
        cannot tell" and "the last run finished" are different answers and
        only refusing is safe to act on; update_repository recovers either
        way (#1705). A marker whose run never reached a graph write
        (`writing=false`) is cleared here and does not refuse.
        """
        try:
            rows = self._ingestor().fetch_all(
                cq.CYPHER_PROJECT_IS_INCOMPLETE,
                {cs.KEY_PROJECT_NAME: project_name},
            )
        except Exception as error:  # noqa: BLE001 -- see docstring
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_UNREADABLE.format(
                    project=project_name, error=error
                )
            )
            # Refuse rather than assume complete. "I cannot tell" and "the
            # last run finished" are different answers, and only one of them
            # is safe to act on: treating an unreadable marker as absent
            # hydrates a scoped reingest from a graph that may be partial,
            # which is the failure this guard exists to prevent. An
            # update_repository recovers either way (#1705).
            return True
        for row in rows or []:
            if not row.get("run_incomplete"):
                continue
            # The marker's phase decides. A run that reached its first graph
            # write may have left the graph partial: refuse. One that stopped
            # before it -- an aborted prologue, a failed embedding purge, a
            # crash in either -- left the graph as it found it and simply
            # could not (or did not live to) clear its own marker. That marker
            # is safe to clear here, and clearing it is what stops a no-write
            # abort from blocking every later process until someone runs a
            # full update (#1705). An absent phase reads as writing, so
            # a marker from before the phase existed stays fail-closed.
            if row.get("writing", True):
                return True
            if not self.recover_stranded(project_name):
                # Could not clear it. The store refused a write, so the run
                # that would follow could not mark itself either; refuse now
                # and let the next attempt retry.
                return True
            # The recovery is CONDITIONAL (it deletes only markers still
            # read-only), and a write reports no affected-row count, so
            # "the write succeeded" does not mean "the marker is gone". A run
            # that promoted its own marker between this method's read and
            # that delete leaves a row the delete skipped, and returning
            # False here would let a scoped reingest proceed over a graph
            # being written (#1850). Re-read and
            # refuse if anything is still outstanding.
            remaining = self.rows(project_name)
            if remaining is None or remaining:
                return True
            logger.warning(
                ls.MCP_INCOMPLETE_MARKER_RECOVERED.format(project=project_name)
            )
        return False

    def begin_writing_or_refuse(self, project_name: str) -> None:
        """`before_write` for `GraphUpdater.reingest`: advance the phase or stop.

        Raising here is caught by the updater and re-raised as
        `ReingestAborted` with the graph untouched, which the registry's `_reingest_sync`
        then classifies as "nothing changed" and clears the marker.
        """
        if (refusal := self.require_writing(project_name)) is not None:
            raise RuntimeError(refusal)

    def recover_then_persisted_incomplete(self, project_name: str) -> bool:
        """Recover a stranded marker, then report the durable state.

        Shared by the hydrating and the retained-updater paths. It has to be
        shared rather than restated: reading the latch WITHOUT first letting
        `persisted_incomplete` clear a recoverable `writing=false` marker
        makes the same project refuse on a warm updater and proceed on a cold
        one, for identical state -- the warm/cold asymmetry this PR already
        fixed once, arriving back in the refusing direction (Greptile,
        PR #1547).

        A widened flag records damage to a SECOND project and this recovery
        is evidence about one, so it may not retire a refusal another project
        earned -- the same rule as the clear in `require_cleared`.
        """
        recoverable_here = self.damage.from_failed_clear == project_name
        persisted_incomplete = self.persisted_incomplete(project_name)
        if (
            not persisted_incomplete
            and recoverable_here
            and not self.damage.unbounded
            and not self.damage.projects
        ):
            self.damage.incomplete = False
            self.damage.project = None
            self.damage.from_failed_clear = None
        return persisted_incomplete
