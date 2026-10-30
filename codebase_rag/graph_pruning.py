"""Remove what the graph still holds of a checkout that the checkout no longer
has."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger

from . import constants as cs
from . import logs as ls
from .parsers.factory import ProcessorFactory
from .services import QueryProtocol
from .services.resource_cleanup import prune_unanchored_resources
from .types_defs import PropertyParams, ResultRow, ResultValue
from .updater.legacy_retirement import LegacyRetirement, repo_scope_params
from .utils import path_utils
from .utils.path_utils import (
    is_inline_module_path,
    vanished,
)


class PruneRun(Protocol):
    """What the orphan prune reads and sets on the sync run it is part of:
    whether the project registry could be read, and whether this run's prune
    was complete, which settles an owed one once its deletes are flushed."""

    registry_unread: bool
    prune_settled: bool


@dataclass
class _PruneTally:
    """What `prune_orphan_nodes` leaves for after its per-label sweeps."""

    registry_skipped: int = 0


class _Orphan(NamedTuple):
    """One node the prune deletes, as it read it."""

    path: str
    key: str
    # A Package is deleted only while it still holds the path it was judged
    # by, so a checkout sharing its project name keeps the one it wrote.
    absolute_path: str


def _orphan(
    path: str, key: str, abs_path: ResultValue | None, label: str, qn: ResultValue
) -> _Orphan:
    """The orphan at `path` as its delete keys it."""
    read_at = abs_path if isinstance(abs_path, str) and abs_path else key
    # Package rows are read by qualified name, and one is deleted by it:
    # two can share an absolute path (issue #2451).
    if label == cs.NodeLabel.PACKAGE and isinstance(qn, str) and qn:
        return _Orphan(path, qn, read_at)
    return _Orphan(path, key, read_at)


class GraphPruner:
    """The orphan prune that closes a sync, and the marker that owes a
    cut-short prune to the next run.

    `owns` says whether this project, and not one nested under it, owns a
    qualified name the graph returned; `delete_module_entities` removes one
    file's Module subtree, scoped to this project; `legacy_retirement` sweeps
    the File nodes an older cgr keyed on an external symlink's target, which
    a complete prune takes with it.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        repo_path: Path,
        project_name: str,
        single_file: Path | None,
        owns: Callable[[str], bool],
        delete_module_entities: Callable[[str], None],
        legacy_retirement: LegacyRetirement,
    ) -> None:
        # The store as a readable graph, None for a write-only sink.
        self._graph = graph
        self._factory = factory
        self._repo_path = repo_path
        self._project_name = project_name
        self._single_file = single_file
        self._owns = owns
        self._delete_module_entities = delete_module_entities
        self._legacy_retirement = legacy_retirement

    def prune_orphan_nodes(self, run: PruneRun, state_dir: Path) -> None:
        """Remove graph nodes whose files/folders no longer exist on disk."""
        graph = self._graph
        if graph is None:
            return
        if self._single_file is not None:
            # The path prune is a whole-project claim (every node whose path
            # is absent from disk), which a run that walked one file cannot
            # make: unguarded it once emptied the graph (#1756). Narrowing it
            # now that single-file runs are rooted correctly is #1776.
            logger.info(ls.PRUNE_SKIPPED_SINGLE_FILE)
            # The two edge-count sweeps still run: they need no walk, and a
            # single-file re-ingest is what strands an ExternalModule or a
            # Resource its target no longer reaches.
            graph.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
            prune_unanchored_resources(graph)
            return

        logger.info(ls.PRUNE_START)
        tally = _PruneTally()
        total_pruned, read_failed = self._prune_labels(graph, run, tally)

        # Rows left for the next healthy run are only swept by one that
        # actually prunes, so owe it through a marker the in-sync fast path
        # refuses on (PR #2125).
        self._record_prune_pending(
            run, state_dir, read_failed or tally.registry_skipped > 0
        )
        if read_failed:
            # The same outage that broke the path reads would break (or act
            # on stale state through) the cleanup below; leave everything
            # for the next healthy run.
            return

        total_pruned += self._legacy_retirement.sweep_file_identities()

        # Drop external import-target modules that no module imports anymore,
        # e.g. an imported name renamed/removed on an incremental rebuild.
        graph.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)

        # Drop shared Resource nodes whose component no longer reaches any
        # code node, e.g. an endpoint whose route changed on a rebuild.
        prune_unanchored_resources(graph)

        if tally.registry_skipped:
            logger.warning(
                ls.PRUNE_SKIPPED_REGISTRY_UNREAD, count=tally.registry_skipped
            )
        if total_pruned:
            logger.info(ls.PRUNE_COMPLETE, count=total_pruned)
        else:
            logger.info(ls.PRUNE_SKIP)

    def _prune_specs(self, repo_abs: str) -> list[tuple[str, PropertyParams, str, str]]:
        """Per label: the read of its nodes, its scope, and its delete."""
        # Scoped to this repository or project: unscoped, the reads pulled
        # every node of every project in the shared graph on each sync with
        # changes (issue #2405). The rows they no longer return were all
        # skipped below anyway, as outside the repository or not owned.
        repo_scope = repo_scope_params(repo_abs)
        project_scope: PropertyParams = {
            cs.KEY_PROJECT_NAME: self._project_name,
            cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
        }
        return [
            (cs.CYPHER_REPO_FILE_PATHS, repo_scope, cs.CYPHER_DELETE_FILE, "File"),
            (
                cs.CYPHER_PROJECT_PRUNABLE_MODULES,
                project_scope,
                cs.CYPHER_DELETE_MODULE,
                "Module",
            ),
            (
                cs.CYPHER_REPO_FOLDER_PATHS,
                repo_scope,
                cs.CYPHER_DELETE_FOLDER,
                "Folder",
            ),
            (
                cs.CYPHER_PROJECT_PACKAGE_PATHS,
                project_scope,
                cs.CYPHER_DELETE_PACKAGE_BY_QN,
                "Package",
            ),
        ]

    def _prune_labels(
        self, graph: QueryProtocol, run: PruneRun, tally: _PruneTally
    ) -> tuple[int, bool]:
        """Delete every label's orphans; how many went, and whether a read
        failed."""
        repo_abs = self._repo_path.resolve().as_posix()
        # A directory is represented by exactly one of Folder and Package,
        # decided by identify_structure this run; the node of the other kind
        # is stale even though the directory exists (issue #1570).
        packages_now = {
            rel.as_posix()
            for rel, qn in self._factory.structure_processor.structural_elements.items()
            if qn
        }

        total_pruned = 0
        read_failed = False
        for query_all, scope, delete_query, label in self._prune_specs(repo_abs):
            try:
                rows = graph.fetch_all(query_all, scope)
            except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
                # A graph that cannot be read cannot be pruned safely; the
                # next healthy run sweeps whatever this one left behind.
                logger.warning(ls.PRUNE_QUERY_FAILED, label=label, error=exc)
                read_failed = True
                continue
            orphans = self._orphan_rows(run, rows, label, repo_abs, packages_now, tally)
            total_pruned += self._delete_orphans(graph, orphans, label, delete_query)
        return total_pruned, read_failed

    def _record_prune_pending(
        self, run: PruneRun, state_dir: Path, pending: bool
    ) -> None:
        """Owe a cut-short orphan prune to the next run, or mark it settled.

        Owing is written at once. Settling only notes that this run's prune
        was complete: the prune covers the whole project every time it runs,
        so it settles whatever an earlier run left, but not until its deletes
        are flushed (`settle_prune_marker`).
        """
        run.prune_settled = not pending
        if not pending:
            return
        try:
            (state_dir / cs.PRUNE_PENDING_FILENAME).touch()
        except OSError:
            # Best effort, like the EXPOSES marker: a read-only tree must not
            # lose the run to a marker it could not write.
            logger.debug(ls.PRUNE_PENDING_NOT_UPDATED)

    def settle_prune_marker(self, run: PruneRun, state_dir: Path) -> None:
        """Remove an owed-prune marker once a complete prune's deletes flush."""
        if not run.prune_settled:
            return
        run.prune_settled = False
        try:
            (state_dir / cs.PRUNE_PENDING_FILENAME).unlink(missing_ok=True)
        except OSError:
            # Best effort, as the write above is: a marker left behind only
            # owes one more prune, and the run it sends past the in-sync fast
            # path settles it again.
            logger.debug(ls.PRUNE_PENDING_NOT_UPDATED)

    def _orphan_rows(
        self,
        run: PruneRun,
        rows: list[ResultRow],
        label: str,
        repo_abs: str,
        packages_now: set[str],
        tally: _PruneTally,
    ) -> list[_Orphan]:
        """Every orphan among one label's rows."""
        orphans = []
        for r in rows:
            orphan = self._orphan_row(run, r, label, repo_abs, packages_now, tally)
            if orphan is not None:
                orphans.append(orphan)
        return orphans

    def _orphan_row(
        self,
        run: PruneRun,
        r: ResultRow,
        label: str,
        repo_abs: str,
        packages_now: set[str],
        tally: _PruneTally,
    ) -> _Orphan | None:
        """One row's node, when it is an orphan."""
        path = r.get("path")
        if not isinstance(path, str) or not path:
            return None
        if is_inline_module_path(path):
            return None
        abs_path = r.get("absolute_path")
        qn = r.get("qualified_name", "")
        if self._foreign_row(path, abs_path, repo_abs):
            return None
        if not self._owned_row(run, qn, tally):
            return None
        key = self._stale_orphan_key(path, abs_path, label, packages_now)
        if key is None:
            return None
        return _orphan(path, key, abs_path, label, qn)

    def _owned_row(self, run: PruneRun, qn: ResultValue, tally: _PruneTally) -> bool:
        """Whether this project owns a row's node, so this prune may judge it."""
        # The root directory's own node is qualified as exactly the
        # project name, with no dot: testing only the dotted prefix
        # dropped it here and let a stale root Package survive beside
        # the Folder that replaced it. `GraphUpdater._package_paths` reads
        # these same rows and admits both forms; the two must agree.
        # Ownership, not the prefix: `svc.` prefixes `svc.v2`'s rows
        # too, and a file only `svc.v2` holds is absent from `svc`'s
        # tree, so the prefix rule pruned it (issue #1985).
        # With the registry unread `owns` degrades to that same
        # prefix rule, so a row it cannot place is left for the next
        # healthy run, which prunes the whole project again
        # (PR #2125). Path-keyed rows carry no qn and
        # still prune.
        if not isinstance(qn, str) or not qn:
            return True
        owned = self._owns(qn)
        if run.registry_unread:
            tally.registry_skipped += 1
            return False
        return owned

    def _foreign_row(
        self, path: str, abs_path: ResultValue | None, repo_abs: str
    ) -> bool:
        """Whether a row's node lies outside this repository, not this prune's.

        Component-aware containment: a bare prefix test would also match a
        sibling root such as <repo>-old (issue #897). A node a build before
        #2451 derived through one of this repository's own links carries its
        target's path, outside for an outside link; the link places it here,
        and nothing derives it any more. Unless that path is `<root>/<path>`:
        that is the node another checkout under this project name writes for
        its own directory at the same relative path, and the upsert shares
        one node between the two, so that checkout's stays even when this
        repository's link points at it (#2451).
        """
        if not isinstance(abs_path, str) or not self._outside_repo_row(
            abs_path, repo_abs
        ):
            return False
        if not path_utils.is_symlink_entry(self._repo_path / path):
            return True
        return abs_path.endswith(f"{cs.SEPARATOR_SLASH}{path}")

    @staticmethod
    def _outside_repo_row(abs_path: str, repo_abs: str) -> bool:
        """Whether a row's absolute path lies outside this repository."""
        return not (abs_path == repo_abs or abs_path.startswith(repo_abs + "/"))

    def _stale_orphan_key(
        self,
        path: str,
        abs_path: ResultValue | None,
        label: str,
        packages_now: set[str],
    ) -> str | None:
        """The delete key of a node that is stale or whose path is gone."""
        is_folder = label == cs.NodeLabel.FOLDER
        stale_kind = (
            (is_folder and path in packages_now)
            or (label == cs.NodeLabel.PACKAGE and path not in packages_now)
            # No node is derived at a link's path any more (issue #2451). A
            # Folder is exempt: one is keyed on its absolute path, and a link
            # to a directory shared its target's node.
            or (not is_folder and path_utils.is_symlink_entry(self._repo_path / path))
        )
        # `vanished`, not `exists()`: since 3.12 `Path.exists()`
        # re-raises PermissionError instead of reading it as
        # absence, so a link whose target sits behind an
        # unreadable directory aborted the whole run HERE -- after
        # the unreadable branch had marked it for retry but before
        # that marker could commit, so the retry this PR adds never
        # landed (PR #1993).
        if stale_kind or vanished(self._repo_path / path):
            # File/Folder deletes key on the absolute path: a sibling
            # project's node can share the relative path (issue #897).
            return (
                abs_path
                if isinstance(abs_path, str) and abs_path
                else (self._repo_path / path).resolve().as_posix()
            )
        return None

    def _delete_orphans(
        self,
        ingestor: QueryProtocol,
        orphans: list[_Orphan],
        label: str,
        delete_query: str,
    ) -> int:
        """Delete one label's orphans; how many were deleted."""
        if orphans:
            logger.info(ls.PRUNE_FOUND, count=len(orphans), label=label)
            for orphan in orphans:
                logger.debug(ls.PRUNE_DELETING, label=label, path=orphan.path)
                if delete_query == cs.CYPHER_DELETE_MODULE:
                    # Module deletes are project-scoped; a sibling
                    # project's module can share the relative path.
                    self._delete_module_entities(orphan.path)
                elif delete_query == cs.CYPHER_DELETE_PACKAGE_BY_QN:
                    self._delete_package(ingestor, orphan.key, orphan.absolute_path)
                else:
                    ingestor.execute_write(delete_query, {cs.KEY_PATH: orphan.key})
        return len(orphans)

    def _delete_package(
        self, ingestor: QueryProtocol, qualified_name: str, absolute_path: str
    ) -> None:
        """Delete one orphan Package of this project, as the prune read it.

        A project name that two checkouts share puts both on one Package
        node, holding the path of whichever wrote it last; only the absolute
        path the prune judged (in this repository, or behind one of its own
        links) may be deleted, so the other checkout's stays (#2451).
        """
        ingestor.execute_write(
            cs.CYPHER_DELETE_PACKAGE_BY_QN,
            {
                cs.KEY_QUALIFIED_NAME: qualified_name,
                cs.KEY_PROJECT_NAME: self._project_name,
                cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
                cs.KEY_ABSOLUTE_PATH: absolute_path,
            },
        )
