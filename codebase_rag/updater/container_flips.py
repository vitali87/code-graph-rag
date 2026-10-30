"""Reconcile the container a directory hangs its files off when a scoped
re-ingest adds or deletes its package indicator (issues #1570, #1798, #1872)."""

from pathlib import Path

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..parsers.factory import ProcessorFactory
from ..parsers.structure_processor import StructureProcessor
from ..services import QueryProtocol
from ..types_defs import ResultRow
from ..utils import path_utils
from ..utils.path_utils import cached_relative_path, cached_resolve_posix
from .tree_scan import TreeScan


class ContainerFlips:
    """The Package or Folder each directory a scoped re-ingest touches is.

    A directory with a package indicator (`__init__.py`, `Cargo.toml`) is a
    Package keyed on a dotted qn; without one it is a Folder keyed on an
    absolute path. These read the kind the graph records, compare it with the
    disk, and after the re-parse prune the node of the kind a directory no
    longer is. `tree_scan` decides which files under a directory the walk
    would index.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        repo_path: Path,
        tree_scan: TreeScan,
    ) -> None:
        self._graph = graph
        self._factory = factory
        self._repo_path = repo_path
        self._tree_scan = tree_scan

    def package_flip(
        self, present: dict[str, Path], gone: dict[str, Path]
    ) -> tuple[set[str], dict[str, Path]]:
        """Directories whose package-ness this call changes, and their files.

        A directory with a package indicator (`__init__.py`, `Cargo.toml`) is
        a Package keyed on a dotted qn; without one it is a Folder keyed on an
        absolute path. Every CONTAINS_FILE and CONTAINS_MODULE edge under it
        hangs off whichever node it is, and that edge is emitted only when the
        module is parsed -- so a directory that changes kind needs its files
        re-parsed to re-point them, exactly as `run()` does for the same
        reason (issue #1570, `GraphUpdater._package_flip_dirs`).

        `reingest` computed none of this: it never re-derived the structure,
        so a deleted `__init__.py` left the Package node and all its edges in
        place, and an ADDED one left the Folder (issue #1798 -- the issue
        reports the deletion, but both directions were broken).

        Returns the flipped directories and the on-disk files under them that
        this call has not already named, for the re-parse.
        """
        structure = self._factory.structure_processor
        indicators = structure.package_indicator_names()
        touched = {
            Path(key).parent.as_posix()
            for key in (*present, *gone)
            if Path(key).name in indicators
        }
        # Narrowed to directories this call actually named an indicator for.
        # Without it a scoped re-ingest reconciles directories the caller
        # never mentioned -- measured: an unrelated `other/` whose
        # `__init__.py` had been deleted on disk by something else was demoted
        # too. That is the whole-project claim `GraphPruner.prune_orphan_nodes`
        # is careful not to make from a partial walk, and a full index is what
        # reconciles the rest.
        if not touched:
            return set(), {}

        # Read-only: `is_package_dir` stats the filesystem and emits nothing.
        # Deriving the structure here instead would WRITE the new container
        # node inside the prologue, and an abort in `before_write` then left
        # the graph holding both container nodes for one directory while
        # reporting that nothing changed (issue #1798). The
        # real derivation happens after the caller's last word.
        flipped = {rel for rel in touched if self._package_ness_changed(structure, rel)}
        if not flipped:
            return set(), {}

        return flipped, self._flip_sibling_files(flipped, present, gone)

    def _flip_sibling_files(
        self, flipped: set[str], present: dict[str, Path], gone: dict[str, Path]
    ) -> dict[str, Path]:
        """On-disk files under a flipped directory that this call has not named.

        They re-parse so their CONTAINS_FILE and CONTAINS_MODULE edges are
        re-emitted onto the container of the new kind: those edges are written
        only when the module is parsed, so a directory that changes kind needs
        its files parsed again or they stay anchored to the old node.
        """
        siblings: dict[str, Path] = {}
        for rel in flipped:
            directory = self._repo_path / rel if rel != "." else self._repo_path
            if not directory.is_dir():
                continue
            for candidate in sorted(directory.iterdir()):
                key = cached_relative_path(candidate, self._repo_path).as_posix()
                # A link is not walked (#2451), so it is not re-parsed here.
                if (
                    not candidate.is_file()
                    or path_utils.is_symlink_entry(candidate)
                    or key in present
                    or key in gone
                    or self._tree_scan.ignores(candidate)
                ):
                    continue
                siblings[key] = candidate
        return siblings

    def _package_ness_changed(self, structure: StructureProcessor, rel: str) -> bool:
        """Whether this directory's kind on DISK differs from the recorded one.

        Read-only: `is_package_dir` stats the filesystem and emits nothing.
        Deriving the structure to answer this would WRITE the new container
        node inside the prologue, and an abort in `before_write` then left the
        graph holding both container nodes for one directory while reporting
        that nothing changed (issue #1798).
        """
        directory = self._repo_path / rel if rel != "." else self._repo_path
        if not directory.is_dir():
            return False
        is_package_now = structure.is_package_dir(directory)
        # The graph's record first; the map only when it cannot answer. See
        # `recorded_kinds` for why the map alone is wrong on a
        # fresh updater.
        recorded = self.recorded_kinds(directory)
        if len(recorded) > 1:
            # Already holds both identities. Whatever is on disk, this needs
            # reconciling -- the derivation plus the prune is the only thing
            # that removes the stale one.
            return True
        if recorded:
            was_package = cs.NodeLabel.PACKAGE.value in recorded
        else:
            was_package = bool(structure.structural_elements.get(Path(rel)))
        return is_package_now != was_package

    def recorded_kinds(self, directory: Path) -> set[str]:
        """Which container labels the GRAPH holds for this directory.

        Empty means "cannot tell" -- no query surface, the read failed, or no
        container node exists yet -- and the caller falls back to the
        in-memory map. TWO entries means the directory already holds both
        identities, which the caller must treat as changed whatever is on
        disk, because reconciling is the only thing that clears it.

        The graph is asked FIRST because the in-memory map is not a record of
        the previous state on every path. A fresh updater (the MCP tool builds
        one per project, and so does the watcher) runs
        `GraphUpdater._hydrate_for_reingest` before the flip check, and that calls
        `identify_structure()`, which derives the map FROM DISK. Both sides of
        the comparison were then the post-change state, no flip was ever
        detected, and the whole fix was inert on the path it exists for
        (PR #1835).
        """
        graph = self._graph
        if graph is None:
            return set()
        try:
            rows = graph.fetch_all(
                cs.CYPHER_CONTAINER_KIND,
                {cs.KEY_PATH: cached_resolve_posix(directory)},
            )
        except Exception:  # noqa: BLE001 -- an unreadable store is not a verdict
            return set()
        return self._kinds_from_rows(rows)

    @staticmethod
    def _kinds_from_rows(rows: list[ResultRow]) -> set[str]:
        """Container labels out of `CYPHER_CONTAINER_KIND` rows.

        Shared by both readers of that query rather than restated in each:
        two readers of one query drift, and this one already carries two
        non-obvious rules that would drift separately.

        EVERY row, not the first: row order is unspecified, and a directory
        can already hold BOTH kinds (#1872), so "the first row wins" answers
        Package or Folder for the same graph depending on the driver
        (PR #1835).
        """
        recorded: set[str] = set()
        for row in rows:
            raw = row.get(cs.KEY_LABELS) if isinstance(row, dict) else None
            # Narrowed to a list of str before the membership test: a result
            # scalar is a union wide enough that `in` is not defined on every
            # member of it, and the driver returns labels as plain strings.
            labels = (
                [item for item in raw if isinstance(item, str)]
                if (isinstance(raw, list))
                else []
            )
            recorded.update(
                label
                for label in labels
                if label in (cs.NodeLabel.PACKAGE.value, cs.NodeLabel.FOLDER.value)
            )
        return recorded

    def uncontained_dirs(self, present: dict[str, Path]) -> tuple[set[str], set[str]]:
        """Ancestor directories of `present` the graph holds no container for.

        Hydration used to emit a node for every directory on disk, which
        covered a directory that is NEW since the last index as a side
        effect. It no longer emits (issue #1872), and the scoped
        re-derivation does not cover these either: it re-derives directories
        whose package-ness FLIPPED, and a brand-new directory never flipped.
        Without this the re-parse writes CONTAINS_FILE and CONTAINS_MODULE
        edges out of a container node nothing wrote -- and in production the
        merge query MATCHes both endpoints, so the edge is not created at all
        and the new file is silently unparented (#1872).

        Guarded on the graph having NO container for the directory, not on
        the directory being in this call. An unconditional ancestor scope
        would re-derive an ancestor that changed independently and give it a
        second container identity -- the #1835 rule, which
        `test_an_independently_changed_ancestor_keeps_one_identity` pins.
        """
        new_dirs: set[str] = set()
        diverged: set[str] = set()
        for key in present:
            self._climb_uncontained(Path(key).parent, new_dirs, diverged)
        return new_dirs, diverged

    def _climb_uncontained(
        self, start: Path, new_dirs: set[str], diverged: set[str]
    ) -> None:
        """Climb from one file's directory to the first correct ancestor.

        Stops at the first directory the graph already records correctly --
        everything above it is correct too, since an ancestor is only wrong
        if something changed it, and that change would have been caught on
        its own climb.

        Split out of `uncontained_dirs` to keep it under the
        cognitive-complexity limit; the nested loop was the whole cost.
        """
        parent = start
        while True:
            rel = parent.as_posix()
            if rel in new_dirs or rel in diverged:
                return
            needs = self._container_needs_deriving(rel)
            if needs is None:
                return
            (diverged if needs else new_dirs).add(rel)
            if rel == ".":
                return
            parent = parent.parent

    def _container_needs_deriving(self, rel: str) -> bool | None:
        """Whether this directory needs deriving, and if so which kind of need.

        `True` means its recorded kind DIVERGED from disk, `False` means the
        graph holds no container for it at all, and `None` means it is
        already correct and the walk can stop climbing.

        The two true cases differ in what the caller must then do: a new
        directory only needs deriving, while a diverged one also needs its
        stale node pruned. Both matter, and the second for a reason easy to
        miss -- a child's parent identity comes from `structural_elements`,
        which hydration fills from DISK, so a child under a diverged ancestor
        gets a containment edge naming an identity the graph does not hold
        and the subtree is left disconnected (PR #1875).
        """
        directory = self._repo_path if rel == "." else self._repo_path / rel
        recorded = self._container_kinds_or_unknown(directory)
        if recorded is None:
            # "I could not ask" is not "there is nothing there". Treating an
            # unreadable store as an absent container would derive every
            # ancestor and re-create the duplicate identity this path
            # prevents. Abort in the read-only prologue, as the other
            # prologue reads do, rather than act on an unknown.
            raise ex.ReingestAborted(ls.REINGEST_CONTAINER_KIND_UNKNOWN)
        if not recorded:
            return False
        return True if self._kind_diverged(directory, recorded) else None

    def _container_kinds_or_unknown(self, directory: Path) -> set[str] | None:
        """The recorded container kinds, or None when the read RAISED.

        ONE read with three outcomes, deliberately: a set (possibly empty,
        meaning no container node), or None meaning "could not ask".
        Asking twice -- once to test readability and once for the value --
        meant a failure BETWEEN the two turned an existing container into
        "no container", which skipped the prune and left both identities in
        the graph: the exact defect this path exists to remove, reachable
        through its own guard (PR #1875).

        A sink with NO query surface is not a failure. It is a legitimate
        configuration the whole scoped path already tolerates, and treating
        it as unknown aborted every re-ingest against one.
        """
        graph = self._graph
        if graph is None:
            return set()
        try:
            rows = graph.fetch_all(
                cs.CYPHER_CONTAINER_KIND,
                {cs.KEY_PATH: cached_resolve_posix(directory)},
            )
        except Exception:  # noqa: BLE001 -- the caller decides what to do
            return None
        return self._kinds_from_rows(rows)

    def _kind_diverged(self, directory: Path, recorded: set[str]) -> bool:
        """Whether the graph's container kind disagrees with the disk's.

        Two recorded kinds is already a disagreement with any disk state --
        one of them is stale whatever the directory is now.
        """
        if len(recorded) > 1:
            return True
        structure = self._factory.structure_processor
        is_package_now = bool(structure.is_package_dir(directory))
        return (cs.NodeLabel.PACKAGE.value in recorded) != is_package_now

    def derivation_scope(self, flipped_dirs: set[str]) -> set[str]:
        """Directories to re-derive: the flipped ones plus their CHILDREN.

        Children, because the prune DETACH DELETEs the node of the old kind
        and that takes its CONTAINS_PACKAGE / CONTAINS_FOLDER edges to child
        containers with it. The sibling re-parse cannot restore those -- a
        child directory is not a file it re-parses -- so re-deriving the
        children re-emits their parent edge onto the surviving node
        (PR #1835).

        Never ANCESTORS. A re-derivation EMITS a node for whatever kind the
        directory is on disk now, so putting an ancestor in scope gave one
        that had changed independently a SECOND container identity beside the
        one it already had. The ancestor walk was there so a nested
        directory's parent lookup could find its enclosing package, and it is
        not needed: `structural_elements` persists across calls, so the
        parent's entry is already in the map.
        """
        scope = set(flipped_dirs)
        for rel in flipped_dirs:
            directory = self._repo_path / rel if rel != "." else self._repo_path
            if not directory.is_dir():
                continue
            scope.update(
                cached_relative_path(child, self._repo_path).as_posix()
                for child in directory.iterdir()
                if child.is_dir()
            )
        return scope

    def prune_flipped(self, flipped_dirs: set[str]) -> None:
        """Delete the node of the kind a flipped directory no longer is.

        A directory is represented by exactly ONE of Folder and Package, and
        the re-parse above has just written the correct one. The other is
        stale even though the directory still exists on disk -- the same
        `stale_kind` rule `GraphPruner.prune_orphan_nodes` applies at the end of
        a full run (issue #1570), scoped here to the directories this call
        flipped.

        Scoped rather than reusing `GraphPruner.prune_orphan_nodes` wholesale:
        that walks every node in the project and deletes anything whose path is
        absent from disk, which is a whole-project claim a scoped re-ingest has
        not earned -- it walked a handful of files and cannot say what the
        project still holds.
        """
        graph = self._graph
        if not flipped_dirs or graph is None:
            return
        elements = self._factory.structure_processor.structural_elements
        for rel in sorted(flipped_dirs):
            directory = self._repo_path / rel if rel != "." else self._repo_path
            absolute = cached_resolve_posix(directory)
            # Keyed on Path(rel), which is Path(".") for the root -- the same
            # key `identify_structure` uses. An earlier version special-cased
            # a falsy `rel`, but `Path(key).parent.as_posix()` yields "." for
            # a root-level file and never "", so that branch was dead and the
            # root was never resolved (issue #1798).
            is_package_now = bool(elements.get(Path(rel)))
            stale = (
                cs.CYPHER_DELETE_FOLDER if is_package_now else cs.CYPHER_DELETE_PACKAGE
            )
            graph.execute_write(stale, {cs.KEY_PATH: absolute})
