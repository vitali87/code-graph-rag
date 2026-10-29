"""Remove what the graph still holds of a checkout that the checkout no longer
has, and retire the identities an older cgr wrote for it."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger

from . import constants as cs
from . import cypher_queries as cq
from . import logs as ls
from .parsers.factory import ProcessorFactory
from .services import QueryProtocol
from .services.resource_cleanup import prune_unanchored_resources
from .types_defs import PropertyParams, ResultRow, ResultValue
from .utils import path_utils
from .utils.path_utils import (
    cached_file_identity_posix,
    is_inline_module_path,
    project_roots_from_rows,
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


class GraphPruner:
    """The orphan prune that closes a sync, the marker that owes a cut-short
    prune to the next run, and the retirement of what an older cgr wrote: the
    pre-#2412 dotted project of this checkout and File nodes keyed on an
    external symlink's target.

    `owns` says whether this project, and not one nested under it, owns a
    qualified name the graph returned; `delete_module_entities` removes one
    file's Module subtree, scoped to this project.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        graph_rows: Callable[[str, PropertyParams | None], list[ResultRow]],
        factory: ProcessorFactory,
        repo_path: Path,
        project_name: str,
        project_named: bool,
        single_file: Path | None,
        owns: Callable[[str], bool],
        delete_module_entities: Callable[[str], None],
    ) -> None:
        # The store as a readable graph, None for a write-only sink, and the
        # updater's reader, which raises for a write-only sink: every read
        # through it takes the raise as a failed read.
        self._graph = graph
        self._graph_rows = graph_rows
        self._factory = factory
        self._repo_path = repo_path
        self._project_name = project_name
        self._project_named = project_named
        self._single_file = single_file
        self._owns = owns
        self._delete_module_entities = delete_module_entities

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
        total_pruned = 0
        tally = _PruneTally()

        repo_abs = self._repo_path.resolve().as_posix()
        # Scoped to this repository or project: unscoped, the reads pulled
        # every node of every project in the shared graph on each sync with
        # changes (issue #2405). The rows they no longer return were all
        # skipped below anyway, as outside the repository or not owned.
        repo_scope = self._repo_scope_params(repo_abs)
        project_scope: PropertyParams = {
            cs.KEY_PROJECT_NAME: self._project_name,
            cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
        }
        prune_specs: list[tuple[str, PropertyParams, str, str]] = [
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
        # A directory is represented by exactly one of Folder and Package,
        # decided by identify_structure this run; the node of the other kind
        # is stale even though the directory exists (issue #1570).
        packages_now = {
            rel.as_posix()
            for rel, qn in self._factory.structure_processor.structural_elements.items()
            if qn
        }

        read_failed = False
        for query_all, scope, delete_query, label in prune_specs:
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

        total_pruned += self._sweep_legacy_file_identities()

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
        if isinstance(qn, str) and qn:
            owned = self._owns(qn)
            if run.registry_unread:
                tally.registry_skipped += 1
                return None
            if not owned:
                return None
        key = self._stale_orphan_key(path, abs_path, label, packages_now)
        if key is None:
            return None
        read_at = abs_path if isinstance(abs_path, str) and abs_path else key
        # Package rows are read by qualified name, and one is deleted by it:
        # two can share an absolute path (issue #2451).
        if label == cs.NodeLabel.PACKAGE and isinstance(qn, str) and qn:
            return _Orphan(path, qn, read_at)
        return _Orphan(path, key, read_at)

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

    @staticmethod
    def _repo_scope_params(repo_abs: str) -> PropertyParams:
        return {cs.KEY_REPO_ROOT: repo_abs, cs.KEY_REPO_PREFIX: repo_abs + "/"}

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

    def _sweep_legacy_file_identities(self) -> int:
        """Delete legacy target-resolved File records, sparing shared keys.

        Legacy pre-GHSA-85gg File nodes were keyed on an external symlink's
        dereferenced TARGET, outside the repository, where the containment
        gate would leak them forever. A key that disagrees with the identity
        derivable from the relative path is such a record; one that agrees
        is a file under a symlinked ancestor directory, legitimate under the
        current scheme (issue #1156). File nodes MERGE globally on
        absolute_path, so the stale key can be the very node another project
        legitimately owns (its repo contains the old link's target); any
        container from outside this repository vetoes the delete.

        Candidates are only out-of-repo Files this project's own containers
        hold, and their containers are read in one query. Every other
        project's File used to be a candidate, each vetoed after its own
        round trip: 5,697 of them for a one-line edit (issue #2405).
        """
        graph = self._graph
        if graph is None:
            return 0
        repo_abs = self._repo_path.resolve().as_posix()
        try:
            rows = self._graph_rows(
                cs.CYPHER_PROJECT_OUTSIDE_FILE_KEYS,
                {
                    **self._repo_scope_params(repo_abs),
                    cs.KEY_PROJECT_NAME: self._project_name,
                },
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.warning(
                ls.PRUNE_QUERY_FAILED, label="legacy File identities", error=exc
            )
            return 0
        candidates = [
            (path, abs_path)
            for row in rows
            if isinstance(path := row.get(cs.KEY_PATH), str)
            and path
            and isinstance(abs_path := row.get(cs.KEY_ABSOLUTE_PATH), str)
            and abs_path
            and abs_path != cached_file_identity_posix(self._repo_path / path)
        ]
        if not candidates:
            return 0
        owned = self._file_keys_owned_only_by_this_project(
            [abs_path for _path, abs_path in candidates], repo_abs
        )
        swept = 0
        for path, abs_path in candidates:
            if abs_path not in owned:
                continue
            logger.debug(ls.PRUNE_DELETING, label="File", path=path)
            graph.execute_write(cs.CYPHER_DELETE_FILE, {cs.KEY_PATH: abs_path})
            swept += 1
        if swept:
            logger.info(ls.PRUNE_LEGACY_IDENTITIES, count=swept)
        return swept

    def _file_keys_owned_only_by_this_project(
        self, keys: list[str], repo_abs: str
    ) -> set[str]:
        """The keys whose every container is this project's, with at least
        one. A sibling project's node can share both the key and the relative
        path (issue #897), and a node whose containers are missing or
        unreadable offers no evidence of sole ownership, so both veto the
        sweep.
        """
        try:
            rows = self._graph_rows(
                cs.CYPHER_FILE_CONTAINERS, {cs.CYPHER_PARAM_PATHS: keys}
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            # Unreadable ownership is unknown ownership: never delete a
            # globally merged key on a failed read, and never let the
            # failure escape mid-update.
            logger.warning(ls.PRUNE_QUERY_FAILED, label="File containers", error=exc)
            return set()
        containers: dict[str, list[ResultRow]] = {}
        for row in rows:
            key = row.get(cs.KEY_FILE_KEY)
            if isinstance(key, str):
                containers.setdefault(key, []).append(row)
        return {
            key
            for key, found in containers.items()
            if all(self._container_is_ours(row, repo_abs) for row in found)
        }

    def _container_is_ours(self, row: ResultRow, repo_abs: str) -> bool:
        """Whether one container row proves THIS project's ownership.

        A foreign Project or Folder, and equally a container with no usable
        identity, reads as not-ours: partial evidence must never delete a
        globally merged key.
        """
        labels = row.get("labels")
        if isinstance(labels, list) and cs.NodeLabel.PROJECT.value in labels:
            return row.get("name") == self._project_name
        container_abs = row.get("absolute_path")
        if isinstance(container_abs, str) and container_abs:
            return container_abs == repo_abs or container_abs.startswith(repo_abs + "/")
        return False

    def retire_legacy_dotted_project(self) -> None:
        """Remove the project this checkout was indexed under before #2412.

        A checkout at `acme.web/` used to default to project `acme.web`,
        whose qualified names alias project `acme`'s package `web`. Syncing
        under the new default would leave that project beside it (review of
        PR 2497). It goes only once this run is committed, so a failed sync
        keeps it; its vectors go first, since they are keyed by node id and
        cannot be found once the nodes are gone, and while they cannot be
        deleted the project stays for the next sync to try again.
        """
        graph = self._graph
        if graph is None:
            return
        legacy = self._legacy_dotted_project()
        if legacy is None:
            return
        try:
            if not self._delete_legacy_embeddings(legacy):
                logger.warning(
                    ls.LEGACY_DOTTED_PROJECT_VECTORS_KEPT.format(legacy=legacy)
                )
                return
            graph.execute_write(
                cq.CYPHER_RETIRE_PROJECT,
                {
                    cs.KEY_PROJECT_NAME: legacy,
                    cs.KEY_PROJECT_PREFIX: f"{legacy}{cs.SEPARATOR_DOT}",
                },
            )
            # What `delete_project` sweeps after its walk: shared nodes the
            # retired project alone anchored.
            graph.execute_write(cs.CYPHER_DELETE_ORPHAN_EXTERNAL_MODULES)
            prune_unanchored_resources(graph)
        except Exception as e:  # noqa: BLE001 -- any engine's driver error; the write is retried later
            # The sync itself succeeded; the next one tries again.
            logger.warning(
                ls.LEGACY_DOTTED_PROJECT_RETIRE_FAILED.format(legacy=legacy, error=e)
            )
            return
        logger.info(
            ls.LEGACY_DOTTED_PROJECT_RETIRED.format(
                legacy=legacy, project=self._project_name
            )
        )

    def _legacy_dotted_project(self) -> str | None:
        """The pre-#2412 project of this checkout, if it can be removed.

        Only an unnamed directory run has one, and only a project whose
        recorded root is this checkout is it. It is kept while another
        project's name is a prefix of it (`acme`) or extends it
        (`acme.web.api`): their qualified names overlap, so some of its nodes
        may be that project's too.
        """
        if self._project_named or self._single_file is not None:
            return None
        legacy = self._repo_path.resolve().name
        if legacy == self._project_name or cs.SEPARATOR_DOT not in legacy:
            return None
        try:
            roots = project_roots_from_rows(
                self._graph_rows(cq.CYPHER_LIST_PROJECTS, None)
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.debug(ls.GRAPH_READ_UNAVAILABLE, label="project roots", error=exc)
            return None
        root = roots.get(legacy)
        if root is None or Path(root).resolve() != self._repo_path.resolve():
            return None
        sharing = sorted(
            name
            for name in roots
            if name != legacy and legacy.startswith(f"{name}{cs.SEPARATOR_DOT}")
        )
        if sharing:
            logger.warning(
                ls.LEGACY_DOTTED_PROJECT_KEPT.format(
                    legacy=legacy,
                    project=self._project_name,
                    sharing=", ".join(sharing),
                )
            )
            return None
        descendants = sorted(
            name for name in roots if name.startswith(f"{legacy}{cs.SEPARATOR_DOT}")
        )
        if descendants:
            logger.info(
                ls.LEGACY_DOTTED_PROJECT_KEPT_FOR_DESCENDANTS.format(
                    legacy=legacy,
                    project=self._project_name,
                    descendants=", ".join(descendants),
                )
            )
            return None
        return legacy

    def _delete_legacy_embeddings(self, legacy: str) -> bool:
        # A failed read raises: without the ids the vectors could never be
        # found again, so the project must stay until a read succeeds. A
        # failed delete, which the vector store logs and swallows, returns
        # False for the same reason (PR #2497).
        rows = self._graph_rows(
            cs.CYPHER_QUERY_PROJECT_NODE_IDS, {cs.KEY_PROJECT_NAME: legacy}
        )
        node_ids = [
            node_id
            for row in rows
            if isinstance(node_id := row.get(cs.KEY_NODE_ID), int)
        ]
        if not node_ids:
            return True
        from .vectors.store import delete_project_embeddings

        return delete_project_embeddings(legacy, node_ids)
