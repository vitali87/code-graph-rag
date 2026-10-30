"""The MCP tools that write the graph under the incomplete-run marker:
indexing, updating and re-ingesting the repository, the structural delta a
write tool appends, and listing, deleting and wiping projects.

Every path that mutates applies the incomplete-run invariants (a), (b) and
(c) that `run_marker.py` states, and the comments below cite the one each
call site keeps.
"""

import json
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from codebase_rag import constants as cs
from codebase_rag import logs as ls
from codebase_rag import tool_errors as te
from codebase_rag.check import delta as sd
from codebase_rag.config import project_ignore_sets
from codebase_rag.exceptions import ReingestAborted
from codebase_rag.gloss.cleanup import prune_orphaned_glosses
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.graph_damage import GraphDamage
from codebase_rag.services import QueryProtocol
from codebase_rag.sync.lock import repo_sync_lock
from codebase_rag.types_defs import (
    DeleteProjectErrorResult,
    DeleteProjectResult,
    DeleteProjectSuccessResult,
    ListProjectsErrorResult,
    ListProjectsResult,
    ListProjectsSuccessResult,
    ReingestToolResult,
)
from codebase_rag.utils.path_utils import derive_project_name, nested_project_names
from codebase_rag.vectors.store import clear_all_embeddings, delete_project_embeddings

from . import constants as mcs
from .handler_base import ToolHandlerBase, run_in_thread


class LifecycleTools(ToolHandlerBase):
    """Index, update, re-ingest, delete and wipe: the graph's writers."""

    async def list_projects(self) -> ListProjectsResult:
        logger.info(ls.MCP_LISTING_PROJECTS)
        try:
            # Serialise against index/update, which delete and rebuild the
            # graph under this lock; an interleaved read mixes generations.
            async with self._ingestor_lock:
                projects = await run_in_thread(self.ingestor.list_projects)
            # A workspace server lists the workspace's projects that are
            # indexed: a workspace repo not yet in the graph is not a project
            # a query can reach, and one outside the workspace is not served.
            if self.workspace is not None:
                allowed = set(self.workspace.project_names())
                projects = [p for p in projects if p in allowed]
            return ListProjectsSuccessResult(projects=projects, count=len(projects))
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_LIST_PROJECTS.format(error=e))
            return ListProjectsErrorResult(error=str(e), projects=[], count=0)

    def _get_project_node_ids(self, project_name: str) -> list[int]:
        rows = self.ingestor.fetch_all(
            cs.CYPHER_QUERY_PROJECT_NODE_IDS,
            {cs.KEY_PROJECT_NAME: project_name},
        )
        result: list[int] = []
        for row in rows:
            node_id = row.get(cs.KEY_NODE_ID)
            if isinstance(node_id, int):
                result.append(node_id)
        return result

    def _nested_projects(self, project_name: str) -> list[str]:
        # A project nested under this one (`svc.v2` under `svc`) keeps its
        # vectors, which the prefix-scoped node-id read also names.
        return nested_project_names(project_name, self.ingestor.list_projects())

    def _delete_project_sync(self, project_name: str) -> DeleteProjectResult:
        projects = self.ingestor.list_projects()
        if project_name not in projects:
            return DeleteProjectErrorResult(
                success=False,
                error=te.MCP_PROJECT_NOT_FOUND.format(
                    project_name=project_name, projects=projects
                ),
            )
        # Before the first write, not after: a delete that fails part way
        # has already removed graph data the retained updater still
        # describes, and the project may still be listed, so only the
        # incomplete flag stops the next reingest from resolving against
        # those definitions over what is left. After a completed delete the
        # project is gone and the not-indexed guard takes over.
        self._live_updater = None
        self.marker.invalidate(project_name)
        # Invariant (a). Nothing has been touched yet, so refusing costs
        # nothing; proceeding would leave a half-removed graph a fresh
        # registry cannot tell from a completed delete.
        # `writing=False` for the same reason as the index path: the
        # embedding purge is not a graph write (#1705).
        if (refusal := self.marker.require(project_name, writing=False)) is not None:
            return DeleteProjectErrorResult(success=False, error=refusal)
        self._cleanup_embeddings_then_begin_writing(project_name)
        self.ingestor.delete_project(project_name)
        # A Gloss is unreachable from the project-delete traversal by design:
        # that is what lets it survive an index's delete-then-rebuild, which
        # deletes and recreates the very symbols it annotates. So the orphan
        # sweep belongs HERE, on the deliberate delete, and not inside
        # `delete_project` itself -- `_index_repository_sync` calls that too,
        # and a sweep there would destroy every gloss on each reindex
        # (issue #1828, #1856). Exactly the rule the
        # incomplete-run marker follows two lines below.
        if isinstance(self.ingestor, QueryProtocol):
            prune_orphaned_glosses(self.ingestor, project_name)
        # Invariant (b). The marker sits on its own node so `delete_project`
        # cannot reach it -- which is what lets it survive an index's
        # delete-then-rebuild -- so a deliberate delete must remove it. The
        # data is gone either way; reporting success while the marker remains
        # blocks every later reingest for a project that no longer exists.
        if (stuck := self.marker.require_cleared(project_name)) is not None:
            return DeleteProjectErrorResult(success=False, error=stuck)
        return DeleteProjectSuccessResult(
            success=True,
            project=project_name,
            message=mcs.MCP_PROJECT_DELETED.format(project_name=project_name),
        )

    async def delete_project(self, project_name: str) -> DeleteProjectResult:
        logger.info(ls.MCP_DELETING_PROJECT.format(project_name=project_name))
        # A workspace server may delete only what it serves (bot review on
        # PR #1972): the destructive path takes the same allow-list as the
        # reads, before any lock or write.
        _scoped, scope_error = self._workspace_scope(project_name)
        if scope_error is not None:
            return DeleteProjectErrorResult(success=False, error=scope_error)
        try:
            async with self._ingestor_lock:
                return await run_in_thread(self._delete_project_sync, project_name)
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_DELETE_PROJECT.format(error=e))
            return DeleteProjectErrorResult(success=False, error=str(e))

    async def wipe_database(self, confirm: bool) -> str:
        if not confirm:
            return mcs.MCP_WIPE_CANCELLED
        logger.warning(ls.MCP_WIPING_DATABASE)
        try:
            async with self._ingestor_lock:
                self._live_updater = None
                self.marker.damage.incomplete = True
                # Not a stranded marker: this flag must not be healed by one.
                self.marker.damage.from_failed_clear = None
                # A wipe spans EVERY project and writes no marker, so its
                # damage is attributable to none of them and no single
                # project's clear may settle it (issue #1774). The success
                # path below clears the flag itself; the failure path widens
                # it to unbounded.
                self.marker.damage.project = None
                await run_in_thread(self.ingestor.clean_database)
                await run_in_thread(clear_all_embeddings)
                # A completed wipe spans every project, so it settles damage
                # to all of them -- the one clear that is entitled to retire a
                # widened flag.
                self.marker.damage = GraphDamage()
            return mcs.MCP_WIPE_SUCCESS
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            # A wipe spans every project and writes no marker, so its damage
            # cannot be recorded under one project's name: an attribution
            # left over from an earlier failure would tell the guards every
            # OTHER project is healthy (PR #1547).
            self.marker.damage.project = None
            # A half-done wipe is the broadest damage there is: it spans every
            # project and writes no marker, so the in-process flag is the only
            # record and NO single project's successful clear may retire it.
            self.marker.damage.unbounded = True
            logger.error(ls.MCP_ERROR_WIPE.format(error=e))
            return te.ToolFailure(mcs.MCP_WIPE_ERROR.format(error=e))

    def _ignore_sets(self) -> tuple[frozenset[str] | None, frozenset[str] | None]:
        # MCP has no `--exclude` flag and no interactive setup, so the
        # repository's own policy is the whole contract here (#1616).
        return project_ignore_sets(Path(self.project_root))

    def _index_repository_sync(self) -> str:
        # Same collision-resistant derivation as the CLI: a bare directory
        # name would let two repos named alike delete each other's graphs.
        project_name = derive_project_name(Path(self.project_root))
        logger.info(ls.MCP_CLEARING_PROJECT.format(project_name=project_name))
        # Before the first write, not after: the delete and the flushes are
        # autocommit writes that can fail part way. From here until the
        # rebuild completes, a later reingest must refuse rather than
        # resolve against the retained updater's definitions or against
        # whatever partial graph a failure left behind.
        self._live_updater = None
        self.marker.invalidate(project_name)
        # Persisted BEFORE the delete, and on a node the delete cannot reach:
        # this path removes the Project and rebuilds it, so a marker stored on
        # the Project would be destroyed by the very operation whose failure it
        # records. A crash between here and the final flush must still refuse
        # the next process's reingest (#1705).
        # A failed MARK aborts before anything destructive happens. The marker
        # is the ONLY protection that survives a restart, so continuing into
        # the delete and rebuild without it means a crash leaves a partial
        # graph that a fresh registry cannot tell from a complete one, and it
        # will happily hydrate a scoped reingest from it. Nothing is lost by
        # stopping here: no graph state has changed yet. That is the opposite
        # of a failed CLEAR, which must not fail a run that already succeeded
        # (#1705).
        #
        # `writing=False`: the embedding purge that follows is not a graph
        # write, so a failure or a crash in it leaves the graph as it was, and
        # a marker saying so lets a fresh process clear it and carry on rather
        # than refuse until a full update (#1705). The phase advances
        # to writing immediately before `delete_project`.
        if (refusal := self.marker.require(project_name, writing=False)) is not None:
            raise RuntimeError(refusal)
        self._cleanup_embeddings_then_begin_writing(project_name)
        self.ingestor.delete_project(project_name)

        self.ingestor.ensure_constraints()
        self.ingestor.flush_all()

        exclude_paths, unignore_paths = self._ignore_sets()
        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        updater.run()
        self.ingestor.flush_all()
        # Cleared only after the flush: the marker must outlive any failure
        # that leaves batches uncommitted.
        #
        # The in-process flag follows the DURABLE state, not the intention. If
        # the clear failed, the graph still says incomplete, and reporting this
        # run complete would leave a fresh registry refusing reingest forever
        # while this one believed all was well. Staying incomplete locally
        # keeps the two in agreement and makes the next update retry the clear
        # (#1705).
        if (stuck := self.marker.require_cleared(project_name)) is not None:
            raise RuntimeError(stuck)
        self._live_updater = updater

        return mcs.MCP_INDEX_SUCCESS_PROJECT.format(
            path=self.project_root, project_name=project_name
        )

    async def _run_ingest(
        self,
        sync_fn: Callable[[], str],
        start_log: str,
        error_log: str,
        error_message: str,
    ) -> str:
        # Indexing and updating differ only in their sync body and their three
        # messages; the lock/thread/error protocol is one implementation.
        logger.info(start_log.format(path=self.project_root))
        try:
            async with self._ingestor_lock:
                return await run_in_thread(self._under_sync_lock, sync_fn)
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(error_log.format(error=e))
            return te.ToolFailure(error_message.format(error=e))

    def _under_sync_lock(self, sync_fn: Callable[[], str]) -> str:
        # Taken before the `:IncompleteRun` marker and the project delete, so
        # a sync refused because another writer holds the checkout changes
        # nothing, not even the marker (issue #2441). `GraphUpdater.run`
        # re-enters it on this thread.
        root = Path(self.project_root)
        with repo_sync_lock(root, derive_project_name(root)):
            return sync_fn()

    async def index_repository(self) -> str:
        return await self._run_ingest(
            self._index_repository_sync,
            ls.MCP_INDEXING_REPO,
            ls.MCP_ERROR_INDEXING,
            mcs.MCP_INDEX_ERROR,
        )

    def _update_repository_sync(self) -> str:
        project_name = derive_project_name(Path(self.project_root))

        # Dropped and marked before the first write: the initial flush
        # commits batches left by earlier calls and can fail part way, and
        # the run itself mutates the graph before it can fail. Either way
        # the retained updater would describe the graph as it was, and a
        # later reingest would resolve against definitions the partial
        # update has already replaced. The incomplete flag makes that
        # reingest refuse until an update completes, since hydrating from
        # the partial graph would be no better.
        self._live_updater = None
        self.marker.invalidate(project_name)
        # The marker goes down BEFORE ensure_constraints, not after.
        # `ensure_constraints` runs `_migrate_legacy_path_keys`, which drops
        # constraints and can run purge queries -- autocommitted, destructive
        # work. Writing the marker after it left a window where a crash
        # during migration produced a damaged graph with nothing recording
        # that a run had started (#1705).
        #
        # It MERGEs its own node, so it works before any Project exists: an
        # earlier version set a property on the Project with MATCH, which
        # updated zero rows on a first index and left a failed first run
        # unmarked.
        #
        # Aborts on failure because the marker is the only protection that
        # survives a restart; nothing has been touched yet, so stopping costs
        # nothing.
        if (refusal := self.marker.require(project_name)) is not None:
            raise RuntimeError(refusal)
        self.ingestor.ensure_constraints()
        self.ingestor.flush_all()

        exclude_paths, unignore_paths = self._ignore_sets()
        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        updater.run()
        self.ingestor.flush_all()
        # Cleared only after the flush: the marker must outlive any failure
        # that leaves batches uncommitted.
        #
        # The in-process flag follows the DURABLE state, not the intention. If
        # the clear failed, the graph still says incomplete, and reporting this
        # run complete would leave a fresh registry refusing reingest forever
        # while this one believed all was well. Staying incomplete locally
        # keeps the two in agreement and makes the next update retry the clear
        # (#1705).
        if (stuck := self.marker.require_cleared(project_name)) is not None:
            raise RuntimeError(stuck)
        self._live_updater = updater
        return mcs.MCP_UPDATE_SUCCESS.format(path=self.project_root)

    def _cleanup_embeddings_then_begin_writing(self, project_name: str) -> None:
        """The step between a `writing=False` mark and the first destructive write.

        The embedding purge has a READ half and a DELETE half, and the phase
        advances between them (#1705, both directions):

        * Reading the project's node ids touches nothing. A failure there
          leaves graph and vectors as they were, so the marker comes off (or,
          failing that, stays `writing=False`) rather than strand a fresh
          process behind a marker for a run that never changed anything.
        * Deleting the vectors IS destructive: a scoped reingest restores
          vectors only for its own paths, so a crash mid-purge followed by a
          fresh process clearing a `writing=False` marker would leave every
          unselected path without embeddings. The phase therefore says
          WRITING before the first vector goes, and a refusal there stops the
          run with nothing deleted.
        """
        try:
            node_ids = self._get_project_node_ids(project_name)
            nested = self._nested_projects(project_name)
        except Exception:
            self.marker.abandon_before_writing(project_name)
            raise
        if (refusal := self.marker.require_writing(project_name)) is not None:
            raise RuntimeError(refusal)
        delete_project_embeddings(project_name, node_ids, nested)

    def _hydrate_reingest_updater(self, project_name: str) -> GraphUpdater:
        """Build a scoped updater when none is retained.

        Split out of `_reingest_sync` to keep it under the
        cognitive-complexity limit; the guards and their reasoning are
        unchanged. Raises rather than returning on a refusal, exactly as
        it did inline.
        """
        # A scoped re-ingest completes a graph; it cannot stand in for
        # the first index. After delete_project or wipe_database the
        # project is gone, and hydrating from nothing would leave every
        # unrelated definition missing.
        # The persisted marker is what makes this survive the process
        # that earned it: with no retained updater this may be a fresh
        # registry after a crash, where `marker.damage.incomplete` is False
        # because nothing in THIS process failed (issue #1679).
        # The durable check runs FIRST and unconditionally, because
        # `marker.persisted_incomplete` is also what CLEARS a recoverable
        # `writing=false` marker. Short-circuiting on `marker.damage.incomplete`
        # skipped that recovery: after a read-only abort whose marker clear
        # failed transiently, `marker.require_cleared` left the flag True and
        # this process refused every later scoped reingest, even once the
        # store became writable again, until a full update or a restart
        # (#1705).
        #
        # Its answer does NOT override the local flag, it joins it. The flag
        # can be the only evidence there is: a failure that could not persist
        # a marker at all leaves the durable state clean while this process
        # knows the graph is partial, and dropping the flag would accept
        # scoped work over it. The flag is cleared only when the recovery
        # above actually cleared the marker, which is exactly the wedged case.
        # Cleared first so the flag reports only THIS call's recovery. As a
        # process-lifetime latch it would go on authorising a clear long after
        # the recovery that set it: an earlier recovered marker, then a later
        # failure that could not persist a marker at all, and the next
        # reingest would drop a `marker.damage.incomplete` that is the only record
        # the graph is partial.
        persisted_incomplete = self.marker.recover_then_persisted_incomplete(
            project_name
        )
        # Scoped exactly as `marker.partial_graph_refusal` scopes a read: a latch
        # attributed to ANOTHER project says nothing about this one, and a
        # scoped reingest is how a project recovers, so refusing here would
        # remove a healthy project's only route back to a complete graph
        # (PR #1547). An unattributed latch still blocks every
        # project, because a wipe-shaped failure could have touched anything.
        latched_here = self.marker.damage.blocks(project_name)
        if latched_here or persisted_incomplete:
            raise ValueError(
                mcs.MCP_REINGEST_AFTER_FAILED_RUN.format(project=project_name)
            )
        if project_name not in self.ingestor.list_projects():
            raise ValueError(mcs.MCP_REINGEST_NEEDS_INDEX.format(project=project_name))
        # Marked after the guards above -- they ask whether a PREVIOUS run
        # left the graph partial, so marking first would make every
        # reingest refuse on its own marker -- and before
        # `ensure_constraints`, which runs the same destructive migration
        # the index and update paths mark before. Invariant (c).
        if (refusal := self.marker.require(project_name)) is not None:
            raise RuntimeError(refusal)
        self.ingestor.ensure_constraints()
        # The same exclusion set the index and update paths use, or an
        # agent-named path under a CLI-excluded directory would be
        # indexed here and kept by every later update.
        exclude_paths, unignore_paths = self._ignore_sets()
        updater = GraphUpdater(
            ingestor=self.ingestor,
            repo_path=Path(self.project_root),
            parsers=self.parsers,
            queries=self.queries,
            unignore_paths=unignore_paths,
            exclude_paths=exclude_paths,
            project_name=project_name,
        )
        self._live_updater = updater
        return updater

    def _reingest_sync(
        self, paths: list[str], deleted: list[str]
    ) -> ReingestToolResult:
        updater = self._live_updater
        # Invariant (a) for EVERY reingest, before either branch does anything.
        # Both branches reach `updater.reingest` below, which mutates, and the
        # hydrating branch additionally runs `ensure_constraints` -- the same
        # destructive migration the index and update paths mark before. The
        # mark used to live INSIDE `if updater is None:`, so a reingest through
        # a retained updater ran entirely unmarked and an interruption left a
        # partial graph a restarted process could not tell from a complete one
        # (#1705).
        #
        # Marking here rather than in each branch is the same lesson as the
        # helpers: enumerate the paths once, at the point they share.
        # The refusal guard must run BEFORE this call marks anything: it asks
        # whether a PREVIOUS run left the graph partial, and marking first
        # would make every reingest refuse on its own marker.
        project_name = derive_project_name(Path(self.project_root))
        # A retained updater marks here, as hydration does inside its helper:
        # both reach the same mutating call below (#1705).
        # `writing=False` because the updater's prologue is read-only: the
        # phase advances through `before_write` below, at the exact point the
        # updater is about to issue its first delete.
        if updater is None:
            updater = self._hydrate_reingest_updater(project_name)
        elif (refusal := self.marker.require(project_name, writing=False)) is not None:
            raise RuntimeError(refusal)
        try:
            report = updater.reingest(
                paths,
                deleted=deleted,
                before_write=lambda: self.marker.begin_writing_or_refuse(project_name),
            )
        except Exception:
            # One classification for every failure, by WHAT HAPPENED rather
            # than by exception type (#1705):
            #
            #   nothing written -> the graph is exactly as it was, so the
            #     marker this call created is a lie about a run that changed
            #     nothing. Clear it, or a fresh process refuses scoped
            #     reingests against a complete graph until someone runs a
            #     full update.
            #   something written -> the graph may be partial. The marker
            #     stays, and the retained updater goes, because it describes
            #     a graph that no longer exists.
            #
            # `reingest_mutated` is set inside `GraphUpdater.reingest` at the
            # exact point its read-only prologue ends. Classifying on
            # ReingestAborted instead would misread a future abort raised
            # mid-run as "nothing changed" and clear a marker that is
            # protecting a partial graph.
            mutated = updater.reingest_mutated
            if mutated:
                self._live_updater = None
                self.marker.invalidate(project_name)
            else:
                # Nothing was written, so the marker this call created is a
                # lie about a run that changed nothing and must come off.
                #
                # The caller's real error (a bad path, an abort) stays
                # primary -- replacing it with a marker error would hide why
                # the reingest actually failed. But a FAILED clear cannot be
                # discarded either: it leaves the project marked incomplete,
                # so every later scoped reingest is refused until a full
                # update runs, for a run that touched nothing (#1705).
                #
                # `marker.abandon_before_writing` syncs `marker.damage.incomplete` to the
                # durable state, so this process stops believing the graph is
                # clean, and the warning it logs is what tells an operator
                # why later reingests refuse. The marker it could not clear
                # still says `writing=False`, so a later process clears it
                # for itself rather than refusing forever (#1705).
                self.marker.abandon_before_writing(project_name)
            raise
        # Invariant (b), as on every other path: both branches marked before
        # mutating, so a completed reingest lifts the marker, and a clear that
        # FAILS is a retryable failure, not a success with the marker kept.
        if (stuck := self.marker.require_cleared(project_name)) is not None:
            raise RuntimeError(stuck)
        return ReingestToolResult(
            reparsed=list(report.reparsed),
            affected=list(report.affected),
            removed=list(report.removed),
            skipped=list(report.skipped),
            elapsed_ms=round(report.elapsed_ms, 1),
        )

    async def reingest(
        self, paths: list[str], deleted: list[str] | None = None
    ) -> ReingestToolResult:
        logger.info(ls.MCP_REINGESTING.format(count=len(paths)))
        try:
            async with self._ingestor_lock:
                return await run_in_thread(
                    self._reingest_sync, list(paths), list(deleted or ())
                )
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            logger.error(ls.MCP_ERROR_REINGEST.format(error=e))
            return ReingestToolResult(
                error=mcs.MCP_REINGEST_ERROR.format(error=e),
                reparsed=[],
                affected=[],
                removed=[],
                skipped=[],
                elapsed_ms=0.0,
            )

    async def update_repository(self) -> str:
        return await self._run_ingest(
            self._update_repository_sync,
            ls.MCP_UPDATING_REPO,
            ls.MCP_ERROR_UPDATING,
            mcs.MCP_UPDATE_ERROR,
        )

    def _delta_after_write(self, paths: list[str]) -> str:
        """The structural delta of a write, as text to append to its result.

        Never fails the write: a project that is not indexed yields nothing,
        and any other failure is reported in place of the delta.
        """
        root = Path(self.project_root)
        relative = sd.normalise_paths(paths, root)
        # Set when the hydration below marks the run, so the success path can
        # lift that marker (invariant b) -- see the comment at the call.
        marked_here: str | None = None
        try:
            if self._live_updater is None and (
                derive_project_name(root) not in self.ingestor.list_projects()
            ):
                return ""
            # The marker-reading hydration, not `_updater_for_reingest`
            # (issue #1783). That helper guards on `marker.damage.incomplete` alone,
            # which lives on the registry and dies with the process: after a
            # crash or an MCP restart it is False because nothing in THIS
            # process failed, so the guard passes and hydrates from a graph a
            # previous run left partial. `_hydrate_reingest_updater` reads the
            # persisted marker as well, which is what survives the process
            # that earned it (#1679).
            #
            # #1705 rebuilt the scoped-reingest call site onto this helper but
            # not this one, which the #1546 stack added after that branch was
            # written -- so the two paths refused and proceeded in the same
            # registry state, and neither call site showed it.
            #
            # Invariant (b) comes with it: that helper MARKS before its
            # destructive migration, so a completed delta has to lift the
            # marker or every later run refuses on it -- a successful delta
            # would durably wedge the project (#1845).
            # `marked_here` stays None until a branch below has marked, so a
            # failure before that point has no marker to lift.
            #
            # BOTH branches mark. The retained-updater branch reaches the same
            # mutating `reingest` below, so leaving it unmarked meant a crash
            # mid-delta left a partially rebuilt graph that a fresh process
            # could not tell from a complete one (#1845).
            # That is the fifth-path gap `_reingest_sync` closed in the #1705
            # review, round 6, arriving on this path too. `writing=False`
            # because the updater's prologue is read-only; the phase advances
            # at its first delete.
            project_name = derive_project_name(root)
            if self._live_updater is not None:
                updater = self._live_updater
                if (
                    refusal := self.marker.require(project_name, writing=False)
                ) is not None:
                    raise RuntimeError(refusal)
                marked_here = project_name
            else:
                marked_here = project_name
                updater = self._hydrate_reingest_updater(marked_here)
            deleted = [p for p in relative if not (root / p).exists()]
            changed = [p for p in relative if p not in deleted]
            delta = sd.observe(
                self.ingestor.fetch_all,
                updater.project_name,
                relative,
                # Invariant (a), second half: the marks above say
                # `writing=False`, and `reingest` starts deleting after its
                # read-only prologue. Without advancing the phase there, a
                # crash mid-delete left a partial graph that a fresh registry
                # reads as recoverable, CLEARS, and hydrates as complete
                # (#1845). Same callback
                # `_reingest_sync` passes for the same reason.
                lambda: updater.reingest(
                    changed,
                    deleted=deleted,
                    before_write=lambda: self.marker.begin_writing_or_refuse(
                        project_name
                    ),
                ),
                repo_root=root,
            )
        except (ValueError, ReingestAborted) as e:
            # Raised before any graph change (validation, an abort while the
            # paths are split): the graph is whole and the updater reusable.
            logger.warning(ls.MCP_DELTA_FAILED.format(error=e))
            # ...so the marker this call took must come off, or a graph that
            # was never touched goes on refusing every later scoped reingest
            # (#1845). `marker.abandon_before_writing` is
            # the same helper `_reingest_sync` uses for its own no-write
            # aborts: it lifts the marker and leaves the attribution to
            # whichever failure owns it.
            if marked_here is not None:
                self.marker.abandon_before_writing(marked_here)
            return "\n\n" + mcs.MCP_DELTA_ERROR.format(error=e)
        except Exception as e:  # noqa: BLE001 -- tool boundary: any failure becomes the tool's error result
            # The re-ingest may have deleted a subtree it never rebuilt: the
            # same invalidation `_reingest_sync` applies, so no later query
            # or write reuses a partial graph (issue #1525).
            logger.warning(ls.MCP_DELTA_FAILED.format(error=e))
            self._live_updater = None
            # This handler writes into THIS server's repository, so the
            # damage is confined to the project that root derives to -- but
            # only NARROWS to it when nothing broader already holds the flag,
            # or a delta failure here would erase another project's
            # marker-less damage (PR #1547).
            self.marker.invalidate(derive_project_name(root))
            # Not a stranded marker: this flag must not be healed by one.
            # This path invalidates precisely BECAUSE a subtree may have been
            # deleted and not rebuilt, so the flag records real damage that no
            # marker describes. Leaving an earlier attribution in place would
            # let an unrelated marker recovery lift it -- the same overwrite
            # the abandon path was fixed for, arriving from the other side.
            # Added on the rebase: this site landed on main (#1525) after the
            # attribution was written, so it had no way to know about it.
            self.marker.damage.from_failed_clear = None
            return "\n\n" + mcs.MCP_DELTA_ERROR.format(error=e)
        if marked_here is not None:
            # Invariant (b): the hydration marked before its constraint
            # migration, so a completed delta must lift it. A failed clear is
            # reported in place of the delta rather than silently retained --
            # this method never raises, so the message is the only channel.
            if (stuck := self.marker.require_cleared(marked_here)) is not None:
                logger.warning(ls.MCP_DELTA_FAILED.format(error=stuck))
                return "\n\n" + mcs.MCP_DELTA_ERROR.format(error=stuck)
        return (
            "\n\n"
            + mcs.MCP_DELTA_HEADER
            + "\n"
            + json.dumps(delta, indent=cs.MCP_JSON_INDENT)
        )
