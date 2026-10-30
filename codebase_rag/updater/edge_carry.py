"""Carry the edges into re-parsed files across their re-parse: the reference
edges unchanged files hold into them and the runtime-observed trace edges,
both read before the old subtrees are deleted and written back after."""

from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Protocol, cast

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..function_registry import FunctionRegistryTrie
from ..parsers.document_tier import DocumentTier
from ..parsers.factory import ProcessorFactory
from ..services import IngestorProtocol, QueryingIngestorProtocol, QueryProtocol
from ..trace.carry import CapturedTraceEdge, capture_trace_edges, carry_trace_edges
from ..types_defs import PropertyDict, ResultRow

_GLOSS_RELS = frozenset(
    {cs.RelationshipType.ANNOTATES.value, cs.RelationshipType.MENTIONS.value}
)


class EdgeCarryRun(Protocol):
    """What the carry reads and sets on the sync run it is part of: whether
    the run re-parses every file, which lets a failed capture degrade where
    an incremental run must abort, and the inbound edges the run holds until
    its deferred stages registered every re-parsed definition."""

    is_full_build: bool
    pending_inbound_edges: list[ResultRow]


def _relinked_go_edge(row: ResultRow) -> bool:
    """A captured IMPLEMENTS or OVERRIDES edge out of a Go file."""
    # Go's are recomputed every run from every method set, read-back ones
    # included (issue #2483), so a pair that still holds comes back without
    # this edge. Restored, one an edit took away stayed: a method in a file
    # the sync never re-parsed kept its OVERRIDES into an interface that had
    # gained a method its type lacks, and a pair that changed source kept
    # the label it had before.
    path = row.get(cs.KEY_CALLER_PATH)
    return (
        row.get(cs.KEY_REL)
        in (cs.RelationshipType.IMPLEMENTS.value, cs.RelationshipType.OVERRIDES.value)
        and isinstance(path, str)
        and path.endswith(cs.EXT_GO)
    )


class EdgeCarry:
    """The inbound and trace edges of the files a sync re-parses.

    Restoring the exact edges an unchanged caller held avoids re-resolving
    it, whose resolution would diverge from a clean index (cgr resolution is
    context-sensitive). `ingestor` is the raw store, which gloss edges and
    the trace carry write to; `sink` the capture-filtering one every other
    restored edge goes through.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        ingestor: IngestorProtocol,
        sink: IngestorProtocol,
        factory: ProcessorFactory,
        document_tier: DocumentTier,
        function_registry: FunctionRegistryTrie,
        repo_path: Path,
        project_name: str,
    ) -> None:
        self._graph = graph
        self._ingestor = ingestor
        self._sink = sink
        self._factory = factory
        self._document_tier = document_tier
        self._function_registry = function_registry
        self._repo_path = repo_path
        self._project_name = project_name

    def capture_inbound_edges(
        self, run: EdgeCarryRun, reindexed_keys: list[str]
    ) -> list[ResultRow]:
        # Record the reference edges unchanged files point at the re-indexed
        # files, BEFORE those files' subtrees (and thus the inbound edges) are
        # deleted. Restoring the exact edges avoids re-resolving the callers,
        # whose resolution would diverge from a clean index (cgr resolution is
        # context-sensitive).
        graph = self._graph
        if not reindexed_keys or graph is None:
            return []
        try:
            return graph.fetch_all(
                cs.CYPHER_INBOUND_EDGES, {cs.CYPHER_PARAM_PATHS: reindexed_keys}
            )
        except Exception:
            # A FULL build re-parses every caller, so the source-derived edges
            # are re-resolved and the sync may continue over an unreadable
            # graph (test_fully_unreadable_graph_still_completes_the_rebuild
            # pins this). The captured set also holds the ANNOTATES / MENTIONS
            # edges of glosses (issue #1808), which have no source to come
            # back from; those are rebuilt from the notes' own recorded names
            # by `GlossReanchor.reanchor` at the end of the run. An incremental run
            # cannot re-resolve edges from files it will not parse, so there
            # the outage aborts the run.
            if not run.is_full_build:
                raise
            logger.warning(ls.INBOUND_CAPTURE_FAILED)
            return []

    def capture_trace_edges(
        self, run: EdgeCarryRun, keys: Iterable[str]
    ) -> list[CapturedTraceEdge]:
        # Read before the subtrees go, as the inbound edges are: a runtime
        # observation lives only in the graph, so the delete would otherwise
        # take it for good (issue #2429).
        paths = sorted(set(keys))
        graph = self._graph
        if not paths or graph is None:
            return []
        try:
            return capture_trace_edges(
                graph, paths, self._project_name + cs.SEPARATOR_DOT
            )
        except Exception:
            # The inbound capture's posture: a full build may go on over an
            # unreadable graph, saying what it lost; an incremental run
            # aborts rather than drop the observations without a word.
            if not run.is_full_build:
                raise
            logger.warning(ls.TRACE_CARRY_CAPTURE_FAILED)
            return []

    def carry_trace_edges(
        self, captured: list[CapturedTraceEdge], gone_paths: Collection[str]
    ) -> None:
        """Re-apply the captured trace edges to the re-parsed graph.

        Never raises, like `GlossReanchor.reanchor`: the sync itself has landed,
        and the observations come back by re-ingesting the trace, which the
        warning tells the user to do.
        """
        if not captured or not isinstance(self._ingestor, QueryingIngestorProtocol):
            return
        try:
            carry_trace_edges(
                self._ingestor, captured, self._repo_path.resolve(), gone_paths
            )
        except Exception as error:  # noqa: BLE001 -- see docstring
            logger.warning(
                ls.TRACE_CARRY_FAILED.format(count=len(captured), error=error)
            )

    def _restorable_edge(
        self, row: ResultRow
    ) -> tuple[tuple[str, str, str], str, tuple[str, str, str]] | None:
        """The (caller_spec, rel, target_spec) a captured row restores, or None.

        None for a malformed row, a target the re-index did not recreate (a
        renamed or removed definition is correctly left without its stale
        inbound edge, matching a clean re-index), a label without a key, or
        a Go type's IMPLEMENTS or OVERRIDES edge, which
        `go_joins.link_interfaces` writes again for every pair that still holds.
        """
        if _relinked_go_edge(row):
            return None
        caller_label = row.get(cs.KEY_CALLER_LABEL)
        caller_qn = row.get(cs.KEY_CALLER_QN)
        rel = row.get(cs.KEY_REL)
        target_label = row.get(cs.KEY_TARGET_LABEL)
        target_qn = row.get(cs.KEY_TARGET_QN)
        if not (
            isinstance(caller_label, str)
            and isinstance(caller_qn, str)
            and isinstance(rel, str)
            and isinstance(target_label, str)
            and isinstance(target_qn, str)
        ):
            return None
        if not self._restore_target_exists(target_label, target_qn):
            return None
        caller_key = cs.NODE_UNIQUE_CONSTRAINTS.get(caller_label)
        target_key = cs.NODE_UNIQUE_CONSTRAINTS.get(target_label)
        if caller_key is None or target_key is None:
            return None
        return (
            (caller_label, caller_key, caller_qn),
            rel,
            (target_label, target_key, target_qn),
        )

    def restore_pending_inbound_edges(self, run: EdgeCarryRun) -> None:
        captured, run.pending_inbound_edges = run.pending_inbound_edges, []
        try:
            if captured:
                # A run stopped before the deferred stages reached the Go receiver
                # methods registers them here, or every held edge into one is
                # dropped for good: the next run re-parses the method's file but
                # not the caller, and the edge is no longer in the graph to
                # capture. A no-op once those stages ran; a stopped run never
                # verifies parent links, so they still follow every registration.
                self._factory.definition_processor.resolve_deferred_go_methods()
        finally:
            # Even when that registration raises (a run stopped BY it retries
            # it here): the edges into every other target are placeable, and
            # nothing captures them again either.
            self.restore_inbound_edges(captured)

    def _restore_target_exists(self, label: str, qualified_name: str) -> bool:
        """Whether a captured edge's target is back after the re-parse.

        A Section is not a definition, so the function registry never holds
        one; the document tier answers for the headings it emitted. A File
        target is never restored: its node outlives the re-parse, and so
        does the edge (issue #2458).
        """
        match label:
            case cs.NodeLabel.MODULE:
                return True
            case cs.NodeLabel.SECTION:
                return self._document_tier.emitted_section(qualified_name)
            case _:
                return qualified_name in self._function_registry

    def restore_inbound_edges(self, captured: list[ResultRow]) -> None:
        # Re-emit each captured inbound edge whose target still exists after the
        # re-index (see `_restorable_edge` for what is dropped).
        if not captured:
            return
        restored = 0
        for row in captured:
            edge = self._restorable_edge(row)
            if edge is None:
                continue
            caller_spec, rel, target_spec = edge
            # The edge's own properties (its site, issue #1522) come back with
            # it: per-site edges are keyed by them, so a bare re-emission
            # would land beside the original instead of restoring it.
            props = row.get(cs.KEY_PROPS)
            # A gloss edge goes to the RAW ingestor. The filtering sink admits
            # what the capture selection lets a parser EMIT, and glosses are
            # never emitted from source: their group is off by default so
            # indexing cannot invent them. Restoring an edge that already
            # existed is not emitting one, and through the sink it would be
            # dropped, orphaning the note (issue #1808).
            writer = self._ingestor if rel in _GLOSS_RELS else self._sink
            writer.ensure_relationship_batch(
                caller_spec,
                rel,
                target_spec,
                properties=cast(PropertyDict, props)
                if isinstance(props, dict) and props
                else None,
            )
            restored += 1
        if restored:
            logger.info(ls.INCREMENTAL_REBUILD_INBOUND, count=restored)
