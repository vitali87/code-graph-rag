"""Retire what an older cgr wrote for a checkout: the pre-#2412 dotted project
it was indexed under, and File nodes keyed on an external symlink's target."""

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import logs as ls
from ..services import QueryProtocol
from ..services.resource_cleanup import prune_unanchored_resources
from ..types_defs import PropertyParams, ResultRow
from ..utils.path_utils import cached_file_identity_posix, project_roots_from_rows


def repo_scope_params(repo_abs: str) -> PropertyParams:
    """The parameters that scope a read to the repository at `repo_abs`."""
    return {cs.KEY_REPO_ROOT: repo_abs, cs.KEY_REPO_PREFIX: repo_abs + "/"}


class LegacyRetirement:
    """The identities a checkout's graph still holds from an older cgr.

    `graph_rows` is the updater's reader, which raises for a write-only
    sink: every read through it takes the raise as a failed read.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        graph_rows: Callable[[str, PropertyParams | None], list[ResultRow]],
        repo_path: Path,
        project_name: str,
        project_named: bool,
        single_file: Path | None,
    ) -> None:
        self._graph = graph
        self._graph_rows = graph_rows
        self._repo_path = repo_path
        self._project_name = project_name
        self._project_named = project_named
        self._single_file = single_file

    def sweep_file_identities(self) -> int:
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
                    **repo_scope_params(repo_abs),
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

    def retire_dotted_project(self) -> None:
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
        legacy = self._dotted_project()
        if legacy is None:
            return
        try:
            if not self._delete_embeddings(legacy):
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

    def _dotted_project(self) -> str | None:
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

    def _delete_embeddings(self, legacy: str) -> bool:
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
        from ..vectors.store import delete_project_embeddings

        return delete_project_embeddings(legacy, node_ids)
