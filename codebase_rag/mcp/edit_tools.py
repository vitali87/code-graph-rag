"""The graph-driven edit operations served over MCP (issue #1532):
`rename`, held to its postcondition contract through a re-ingest behind the
incomplete-run marker.
"""

from collections.abc import Callable
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.types_defs import ReingestReport

from . import constants as mcs
from .lifecycle_tools import LifecycleTools


class EditTools(LifecycleTools):
    """Rename through the graph, re-ingesting what it wrote."""

    async def rename(
        self,
        qualified_name: str,
        new_name: str,
        allow_heuristic: bool = False,
        dry_run: bool = False,
        project: str | None = None,
    ) -> object:
        # The graph read and the edit share the ingestor lock (taken by
        # `_graph_query`): a rebuild landing between planning and writing
        # would rewrite stale sites.
        return await self._graph_query(
            cs.MCPToolName.RENAME,
            project,
            lambda name: self._run_rename(
                name, qualified_name, new_name, allow_heuristic, dry_run
            ),
        )

    def _rename_reingest_updater(self, project_name: str) -> GraphUpdater:
        """The updater the rename re-ingests through, refusing a partial graph.

        A retained updater for THIS project is reused as-is: it is warm, and
        the guarded callback does its own marking. Otherwise hydrate through
        the marker-reading helper, never a flag-only one -- `_updater_for_
        reingest` was deleted under #1783 precisely because its guard reads
        `marker.damage.incomplete` alone, which dies with the process, so a fresh
        registry after a crash hydrated from a graph a previous run left
        partial (#1679).

        The reuse therefore has to carry the same refusal itself. Skipping it
        re-created that very defect on this path: `marker.require` WRITES a
        marker and never reads one, and `_hydrate_reingest_updater` is the
        only place `marker.persisted_incomplete` is consulted, so a warm updater
        re-ingested over a graph a PEER REGISTRY left partial -- identical
        incomplete state giving opposite outcomes purely on whether an updater
        happened to be warm. Two registries per project is a real
        configuration (#1709), and RENAME sits outside `_READS_THE_GRAPH`, so
        the read guard does not cover it either (PR #1547).

        Split out of `_guarded_rename_reingest` to keep it under the
        cognitive-complexity limit; choosing the updater and refusing over a
        partial graph is one decision, and wrapping the call is another.
        """
        retained = self._live_updater
        if retained is None or retained.project_name != project_name:
            return self._hydrate_reingest_updater(project_name)
        # Recovery FIRST, then the latch: reading the latch before
        # `marker.persisted_incomplete` can clear a stranded recoverable marker left
        # this path refusing a rename the cold path allows.
        persisted_incomplete = self.marker.recover_then_persisted_incomplete(
            project_name
        )
        latched_here = self.marker.damage.blocks(project_name)
        if latched_here or persisted_incomplete:
            raise ValueError(
                mcs.MCP_REINGEST_AFTER_FAILED_RUN.format(project=project_name)
            )
        return retained

    def _guarded_rename_reingest(
        self, project_name: str
    ) -> Callable[[list[str]], ReingestReport] | None:
        """The rename's re-ingest callback, behind the incomplete-run marker.

        Returns None when the project has no graph to measure against.

        The callback was previously passed RAW, so a process death part way
        through it left a partial graph that a restarted server treated as
        complete -- the exact failure the marker exists to prevent, on the one
        path that did not use it (PR #1547). Same shape as
        `_reingest_sync`: mark before the first write, clear after, and refuse
        rather than start if the marker cannot be written.

        `writing=False` at the mark, because the updater's prologue is
        read-only; `before_write` advances the phase at the exact point the
        first delete is about to be issued.
        """
        if (
            self._live_updater is None
            and project_name not in self.ingestor.list_projects()
        ):
            return None
        updater = self._rename_reingest_updater(project_name)

        def guarded(paths: list[str]) -> ReingestReport:
            if (
                refusal := self.marker.require(project_name, writing=False)
            ) is not None:
                raise RuntimeError(refusal)
            try:
                report = updater.reingest(
                    paths,
                    before_write=lambda: self.marker.begin_writing_or_refuse(
                        project_name
                    ),
                )
            except Exception:
                # Classified exactly as `_reingest_sync` does, which is what
                # "same shape" above is supposed to mean. Clearing in a
                # blanket `finally` was the bug: the rename recovers this
                # signal only on the ROLLBACK path, where the report carries
                # `graph_incomplete` and `_run_rename` re-marks durably. The
                # INITIAL postcondition re-ingest fails inside `measure()`,
                # which returns `verdict=None` and sets no such flag, so a
                # mutating failure there dropped the durable marker, raised
                # nothing, and returned `applied` over a possibly-partial
                # graph (PR #1547).
                if updater.reingest_mutated:
                    # The marker STAYS: the graph may be partial and it is the
                    # only record that survives this process. The retained
                    # updater goes because it describes a graph that is gone,
                    # and the healing licence is revoked for the same reason
                    # as the rollback site -- an earlier failed clear does not
                    # explain damage this call just did.
                    self._live_updater = None
                    self.marker.invalidate(project_name)
                else:
                    # Nothing was written, so the marker this call created is
                    # a lie about a run that changed nothing: leaving it up
                    # would refuse every later run for a failure already
                    # reported. This direction is not a nicety -- an earlier
                    # round of this PR shipped a marker with no expressible
                    # exit and refused every later run for the process
                    # lifetime, which was worse than the leak it replaced.
                    self.marker.require_cleared(project_name)
                raise
            self.marker.require_cleared(project_name)
            return report

        return guarded

    def _run_rename(
        self,
        project_name: str,
        qualified_name: str,
        new_name: str,
        allow_heuristic: bool,
        dry_run: bool,
    ) -> object:
        from codebase_rag.editing.rename import RenameRefused, rename, sites_for

        # The selected project's relative paths are meaningful only under
        # the root it was indexed from; another project's paths must not be
        # edited beneath this server's repository (issue #1542).
        root = graph_query.source_root_for(
            self.ingestor.fetch_all, project_name, Path(self.project_root)
        )
        if root is None:
            return {
                cs.DICT_KEY_ERROR: cs.RENAME_WRONG_ROOT.format(project=project_name)
            }
        try:
            # The applied rename is held to its postcondition contract through
            # the scoped re-ingest (issue #1531); a project that is not indexed
            # has no graph to measure against and skips it.
            # The selected project was verified to be indexed from this
            # root above, and the guarded callback rejects a cached updater
            # built for a different project, so the delta is measured under
            # the same name the re-ingest writes.
            reingest = self._guarded_rename_reingest(project_name)
            report = rename(
                self.ingestor.fetch_all,
                project_name,
                root,
                qualified_name,
                new_name,
                allow_heuristic=allow_heuristic,
                dry_run=dry_run,
                reingest=reingest,
                heuristic_opt_in=cs.MCPParamName.ALLOW_HEURISTIC,
            )
        except RenameRefused as refused:
            return {
                cs.DICT_KEY_ERROR: str(refused),
                cs.KEY_AMBIGUOUS: sites_for(refused.ambiguous),
                cs.KEY_UNLOCATABLE: list(refused.unlocatable),
                cs.KEY_UNPLANNED: sites_for(refused.unplanned),
            }
        payload_marker_error: str | None = None
        if getattr(report, "graph_incomplete", False):
            # The rollback's re-ingest failed: the same invalidation the
            # scoped re-ingest applies, so no later call reuses a partial graph.
            self._live_updater = None
            # Narrows to this project only when nothing broader holds the
            # flag, and revokes any healing licence: the rollback's damage
            # is new, so no earlier failed clear explains it.
            self.marker.invalidate(project_name)
            # ...and DURABLY, because that flag dies with this process while
            # the half-restored graph does not. A fresh registry would see a
            # project that looks whole, serve reads from it and hydrate a
            # scoped updater from the partial graph (PR #1547).
            # `writing=True`: the failed re-ingest had already begun mutating.
            payload_marker_error = self.marker.require(project_name, writing=True)
        payload = dict(report._asdict())
        payload[cs.KEY_SITES] = sites_for(report.sites)
        payload[cs.KEY_AMBIGUOUS] = sites_for(report.ambiguous)
        payload[cs.KEY_UNPLANNED] = sites_for(report.unplanned)
        payload["verdict"] = report.verdict._asdict() if report.verdict else None
        if payload_marker_error is not None:
            # The graph is partial AND the record of it could not be written.
            # Reported, never swallowed: this needs operational recovery and
            # a restarted process cannot infer it. A warning, not an error:
            # the rename itself was applied.
            payload[cs.DICT_KEY_WARNING] = payload_marker_error
        return payload
