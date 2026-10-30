"""Pass 2 of a sync: hash the walk, re-parse every file that changed and every
file whose edges the change moves, and reconcile the files that are gone."""

import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import NamedTuple, Protocol

from loguru import logger
from rich.progress import Progress, SpinnerColumn, TaskID, TextColumn
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..parsers.factory import ProcessorFactory
from ..services import IngestorProtocol, QueryProtocol
from ..trace.carry import CapturedTraceEdge
from ..types_defs import ResultRow
from ..utils.path_utils import (
    cached_file_identity_posix,
    cached_relative_path,
    vanished,
)
from .dependents import DependentLookup, DependentRun
from .edge_carry import EdgeCarry, EdgeCarryRun
from .file_parse import FileParser
from .frontends import FrontendRunner
from .generated_sources import GeneratedSources
from .module_seeding import ModuleQnSeeding, stem_key
from .tree_scan import FileScan, HashBaseline, TreeScan, TreeScanRun
from .types_defs import FileStateRemover


class FilePassRun(TreeScanRun, DependentRun, EdgeCarryRun, Protocol):
    """What the pass reads and sets on the sync run it is part of, beyond the
    scan's and the lookups': the trace edges read before the re-parsed
    subtrees are deleted, re-applied once Pass 3 rebuilt the static edges
    (issue #2429), and the files deleted, whose edges must not follow their
    qualified names onto a same-stem survivor."""

    captured_trace_edges: list[CapturedTraceEdge]
    trace_gone_paths: frozenset[str]


class _Walk(NamedTuple):
    """The checkout as the hash pass found it."""

    baseline: HashBaseline
    eligible_files: list[tuple[Path, str]]
    flux_stems: set[str]
    preexisting_paths: frozenset[str] | None
    scan: FileScan


class _Reparse(NamedTuple):
    """The files the re-parse covers beside the changed ones, and the files
    whose subtrees go before it."""

    eligible_by_key: dict[str, Path]
    reindexed_keys: list[str]
    deleted_before_parse: list[str]
    added_keys: list[str]


def _interface_sources(
    changed_entries: list[tuple[Path, str, bool, bytes]], excluded: Collection[str]
) -> list[tuple[Path, str, bytes]]:
    """The re-indexed files whose interface may have held, with the bytes the
    parse will read: a file added by the run has no stored interface, and a
    flux-stem or package-flip sibling's module qn itself may move."""
    return [
        (path, key, data)
        for path, key, is_new, data in changed_entries
        if not is_new and key not in excluded
    ]


def _reindexed_keys(changed_entries: list[tuple[Path, str, bool, bytes]]) -> list[str]:
    """The changed files that were indexed before this run, sorted."""
    return sorted(
        file_key for _fp, file_key, is_new, _b in changed_entries if not is_new
    )


def _added_and_modified_entries(
    changed_entries: list[tuple[Path, str, bool, bytes]], is_full_build: bool
) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]]:
    """The added and the modified files' bytes, for the waiter lookups."""
    # An ADDED file has no inbound edges for the caller query to follow:
    # the files that referenced it while it was missing recorded the
    # names they could not resolve, and are found from those (issue
    # #1568). The importer lookup existed for the scoped path only.
    # On a full build every file is new and the graph holds no waiter,
    # so the lookup (which parses each added file once more) is skipped.
    added_entries = (
        []
        if is_full_build
        else [
            (file_key, file_bytes)
            for _fp, file_key, is_new, file_bytes in changed_entries
            if is_new
        ]
    )
    modified_entries = (
        []
        if is_full_build
        else [
            (file_key, file_bytes)
            for _fp, file_key, is_new, file_bytes in changed_entries
            if not is_new
        ]
    )
    return added_entries, modified_entries


def go_package_dirs(keys: Iterable[str]) -> set[str]:
    """The directories of the Go files among `keys`: their packages."""
    return {
        PurePosixPath(key).parent.as_posix() for key in keys if key.endswith(cs.EXT_GO)
    }


def _go_package_siblings(
    keys: Iterable[str], eligible_by_key: Mapping[str, Path]
) -> list[str]:
    """The walked Go files beside a Go file the sync re-parses or deletes.

    A Go method hangs off its receiver type, which another file of the
    package may declare, so re-parsing the type's file deleted the methods
    of every file beside it that the sync did not re-parse, and re-parsing a
    method's file alone could not find its type and filed the method under
    a second name beside the stale one. Package files re-parse together,
    and the type's methods, a build's declarations of one among them, come
    back as a clean index has them (issue #2483).
    """
    keys = set(keys)
    dirs = go_package_dirs(keys)
    return sorted(
        key
        for key in eligible_by_key
        if key not in keys
        and key.endswith(cs.EXT_GO)
        and PurePosixPath(key).parent.as_posix() in dirs
    )


def _advance_past_settled(progress: Progress, task: TaskID, scan: FileScan) -> None:
    """Advance the bar past the files the hash pass settled without a parse."""
    if scan.skipped_count or scan.unreadable_count:
        progress.advance(task, scan.skipped_count + scan.unreadable_count)


class FilePass:
    """Pass 2 of `GraphUpdater.run`: the definitions of every file the sync
    re-parses, with the old subtrees cleared first and the files that left
    the walk reconciled after.

    `remover` is the updater whose in-memory state a re-parsed or deleted
    file leaves, `delete_module_entities` deletes one file's subtree from
    the graph, and `package_flip_dirs` names the directories whose
    package-ness changed since the run began.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        ingestor: IngestorProtocol,
        factory: ProcessorFactory,
        repo_path: Path,
        single_file: Path | None,
        tree_scan: TreeScan,
        module_seeding: ModuleQnSeeding,
        dependent_lookup: DependentLookup,
        edge_carry: EdgeCarry,
        file_parser: FileParser,
        frontend_runner: FrontendRunner,
        generated_sources: GeneratedSources,
        remover: FileStateRemover,
        delete_module_entities: Callable[[str], None],
        package_flip_dirs: Callable[[], set[str]],
    ) -> None:
        self._graph = graph
        self._ingestor = ingestor
        self._factory = factory
        self._repo_path = repo_path
        self._single_file = single_file
        self._tree_scan = tree_scan
        self._module_seeding = module_seeding
        self._dependent_lookup = dependent_lookup
        self._edge_carry = edge_carry
        self._file_parser = file_parser
        self._frontend_runner = frontend_runner
        self._generated_sources = generated_sources
        self._remover = remover
        self._delete_module_entities = delete_module_entities
        self._package_flip_dirs = package_flip_dirs

    def process_files(
        self, run: FilePassRun, state_dir: Path, force: bool, reparsed_keys: set[str]
    ) -> None:
        """Run the pass; `reparsed_keys` is refilled with the files it parses."""
        self._factory.import_processor.rust.reset_rust_path_caches()
        self._factory.import_processor.java.reset_java_path_caches()
        self._factory.import_processor.julia.reset_julia_path_caches()
        self._factory.import_processor.dart.reset_dart_path_caches()
        walk = self._hash_walk(run, state_dir, force)
        scan = walk.scan
        reparse = self._plan_reparse(run, walk)
        # A caller that lives in a DELETED file must not be restored: its
        # module is gone, and if a surviving same-stem sibling has just taken
        # over its qn the restore would hang the dead file's edge on the
        # survivor (issue #1569).
        deleted_set = set(reparse.deleted_before_parse)
        captured_inbound = self._captured_inbound_edges(
            run, reparse.reindexed_keys, deleted_set
        )
        run.captured_trace_edges = self._edge_carry.capture_trace_edges(
            run, (*reparse.reindexed_keys, *reparse.deleted_before_parse)
        )
        run.trace_gone_paths = frozenset(deleted_set)
        reparsed_keys.clear()
        reparsed_keys.update(
            file_key for _fp, file_key, _new, _b in scan.changed_entries
        )

        pre_parsed, first_failure = self._clear_stale_subtrees(
            walk, reparse, reparsed_keys
        )
        changed_count, first_failure = self._parse_changed_files(
            walk, pre_parsed, first_failure
        )

        deleted_keys = self._tree_scan.deleted_keys_for_run(
            walk.baseline.old_hashes, walk.preexisting_paths, scan
        )
        self._delete_reconciled_files(deleted_keys, deleted_set)

        # Restored by `run()` after the deferred stages, not here: a Go
        # receiver method joins the registry only there, so every edge into
        # one was dropped as a target the re-index had not recreated, and
        # the unchanged caller is never re-parsed to rebuild it (#3270).
        run.pending_inbound_edges = captured_inbound

        scan.log_counts(changed_count)
        self._file_parser.log_unparsable_manifests()
        if first_failure is not None:
            raise first_failure

        # Deferred to the post-flush commit point in `run` (issue #1615). The
        # deletions above are execute_write calls that only become durable at
        # that flush; committing the cache here would let a run that died in
        # between tell its successor the file was already reconciled, and the
        # successor computes an empty `deleted_keys` and never re-issues the
        # delete. The subtree then stays in the graph until a full rebuild.
        self._tree_scan.stash_pending_caches(run, state_dir, scan, walk.baseline)
        # Stamp only full builds: re-stamping an incremental run would
        # silence the staleness warning while unchanged files still carry
        # the old parser's edges. Stashed here and WRITTEN at the commit point
        # in `run`, after the final flush (issue #1634): a build that dies
        # between here and that flush must leave no stamp, or its successor
        # reads its own fingerprint back and never warns.
        # A parser-changed project run re-parsed every file too, so it stamps
        # the fingerprint it parsed under; a single-file run did not, and
        # leaves the stale stamp for the next project run (issue #1977).
        # Only a run that actually covered the project may vouch for the
        # parser it parsed under. An unknown graph state means the delete-
        # before-reingest could not be scoped, so some old parser's edges
        # survive, and stamping anyway makes the NEXT run read back a
        # matching fingerprint and fast-path over exactly those rows --
        # permanently. A file this run could not read keeps its
        # old subtree too, but one carrying the unreadable mark cannot be
        # fast-pathed over: the next run refuses the in-sync check on the
        # mark and re-parses the file under the current parser, old subtree
        # deleted first (#1983). Holding the stamp back for it would re-index
        # every file to repair one. An unreadable file left UNMARKED (its
        # path is gone) still holds the stamp back.
        self._tree_scan.stash_parser_fingerprint(
            run, state_dir, scan, walk.baseline.is_full_build
        )

    def _hash_walk(self, run: FilePassRun, state_dir: Path, force: bool) -> _Walk:
        """Walk the checkout and hash every file against the last run's cache."""
        baseline = self._tree_scan.hash_baseline(run, state_dir, force)
        is_full_build = baseline.is_full_build
        # Before the parse, which re-records every pair a full build holds.
        if is_full_build:
            self._factory.definition_processor.reset_interface_implementers()

        eligible_files = self._tree_scan.collect_eligible_files()

        flux_stems = self._module_seeding.prepare_flux_stems(
            eligible_files, baseline.old_hashes, is_full_build
        )
        preexisting_paths = self._tree_scan.preexisting_paths(
            run, state_dir, force, baseline
        )
        scan = FileScan()

        # Taken BEFORE the loop below, so it is at or earlier than every hash
        # it describes. `_is_already_in_sync` skips rehashing any file whose
        # mtime is at or below the CACHE FILE's own mtime, so deferring the
        # write must not defer that stamp: a file edited after its hash was
        # taken but before the commit point would otherwise be stamped newer
        # than its own edit and skipped for good (issue #1615).
        # Capturing here rather than after the loop also covers a file edited
        # between its own hash and the end of the loop. Erring early only ever
        # costs a redundant rehash; erring late loses an edit.
        run.pending_cache_observed_at = time.time()

        # A single-file target that existed at construction and is gone by
        # now is a DELETION, not an unreadable file (issue #1737). Left on the
        # unreadable path the run logged "Skipped 1 unreadable files",
        # returned normally and republished a cache still naming the file,
        # with the file's definitions still in the registry: a caller of the
        # file-target constructor got a success signal for a file that was
        # not indexed (the orphan prune had already removed its graph nodes).
        # Its state, nodes and cache entry go the way `deleted_keys` takes
        # them below.
        scan.single_gone_key = self._tree_scan.single_target_gone_before_scan(
            eligible_files
        )
        if scan.single_gone_key is not None:
            eligible_files = []

        for filepath, file_key in eligible_files:
            self._tree_scan.scan_eligible_file(
                filepath, file_key, force, baseline, preexisting_paths, scan
            )

        self._module_seeding.restore_unreadable_flux_claims(scan.unreadable_keys)
        return _Walk(baseline, eligible_files, flux_stems, preexisting_paths, scan)

    def _plan_reparse(self, run: FilePassRun, walk: _Walk) -> _Reparse:
        """Join every file whose edges the change moves to the re-parse set."""
        scan = walk.scan
        is_full_build = walk.baseline.is_full_build
        # Before deleting any changed file's subtree (which removes the inbound
        # CALLS/IMPORTS/INSTANTIATES edges incident on it), capture those edges
        # so they can be restored verbatim afterwards (issue #532, inbound
        # half). New files have no prior inbound edges, so only re-indexed
        # (changed, non-new) files matter.
        reindexed_keys = _reindexed_keys(scan.changed_entries)
        # A file depending on a re-indexed one can have its bindings moved by
        # the change (a new override shadowing an inherited method); restoring
        # its old edges verbatim would freeze the stale binding, so it joins
        # the re-parse set BEFORE capture: the capture query then excludes
        # edges among the expanded set and Pass 3 recomputes them, while its
        # own inbound edges are captured and restored like any re-indexed
        # file's (issue #1229 phase 4).
        present = {file_key for _fp, file_key, _new, _b in scan.changed_entries}
        eligible_by_key = {file_key: fp for fp, file_key in walk.eligible_files}
        deleted_before_parse = self._tree_scan.keys_deleted_before_parse(
            walk.baseline, walk.preexisting_paths, eligible_by_key, scan.unreadable_keys
        )
        added_entries, modified_entries = _added_and_modified_entries(
            scan.changed_entries, is_full_build
        )
        added_keys = [key for key, _b in added_entries]
        reindexed_keys = self._queue_affected_callers(
            self._change_dependents(
                run,
                walk,
                reindexed_keys,
                deleted_before_parse,
                eligible_by_key,
                (added_entries, modified_entries),
                present,
            ),
            present,
            eligible_by_key,
            scan,
            reindexed_keys,
        )
        reindexed_keys = self._queue_affected_callers(
            _go_package_siblings({*present, *deleted_before_parse}, eligible_by_key),
            present,
            eligible_by_key,
            scan,
            reindexed_keys,
        )
        reindexed_keys = self._queue_foreign_members(
            run, reindexed_keys, deleted_before_parse, present, eligible_by_key, scan
        )
        # Pass 2 order decides which same-stem file claims the bare module
        # qn; a clean build processes files in walk order, so the re-parse
        # set follows it too instead of the order the dependents were found.
        eligible_order = {key: i for i, (_fp, key) in enumerate(walk.eligible_files)}
        scan.changed_entries.sort(key=lambda entry: eligible_order.get(entry[1], -1))
        return _Reparse(
            eligible_by_key, reindexed_keys, deleted_before_parse, added_keys
        )

    def _change_dependents(
        self,
        run: FilePassRun,
        walk: _Walk,
        reindexed_keys: list[str],
        deleted_before_parse: list[str],
        eligible_by_key: dict[str, Path],
        changed_entries: tuple[list[tuple[str, bytes]], list[tuple[str, bytes]]],
        present: set[str],
    ) -> list[str]:
        """The files a re-indexed, deleted or flux-stem file sends back for a
        re-parse, and the ones waiting on an added or modified file."""
        added_entries, modified_entries = changed_entries
        siblings = self._flux_siblings(
            eligible_by_key, walk.flux_stems, walk.baseline.is_full_build
        )
        # The siblings join the dependents query: a flux-stem survivor's
        # module qn changes on this run, so a file that includes or calls it
        # must re-parse too, or its captured inbound edges are restored
        # against the old qn (a wrong IMPORTS edge, a dropped CALLS edge).
        # A file that DEFINES into a departing module joins for a different
        # reason than a caller does: its definitions live in the subtree
        # about to be deleted, and no edge points from it into that file for
        # the caller query to follow (issue #1660).
        foreign_definers = self._dependent_lookup.foreign_definer_keys(
            {*reindexed_keys, *deleted_before_parse}
        )
        stable = self._dependent_lookup.interface_stable_modules(
            run,
            _interface_sources(
                walk.scan.changed_entries, {*deleted_before_parse, *siblings}
            ),
        )
        waiters = {
            *self._dependent_lookup.unresolved_importer_keys(
                [key for key, _b in added_entries]
            ),
            *self._dependent_lookup.unresolved_reference_waiters(
                added_entries, modified_entries
            ),
            *self._dependent_lookup.dart_package_waiters(
                run, {*present, *deleted_before_parse}
            ),
        }
        # A foreign definer's definitions are rebuilt with the module it
        # defines into, so their callers are its dependents as a re-indexed
        # file's are.
        return sorted(
            {
                *self._dependent_lookup.dependent_keys(
                    sorted(
                        {
                            *reindexed_keys,
                            *deleted_before_parse,
                            *siblings,
                            *foreign_definers,
                        }
                    ),
                    stable,
                    waiters,
                ),
                *siblings,
                *foreign_definers,
                *waiters,
            }
        )

    def _clear_stale_subtrees(
        self, walk: _Walk, reparse: _Reparse, reparsed_keys: set[str]
    ) -> tuple[dict[Path, tuple[Node, dict[str, list] | None]], Exception | None]:
        """Pre-parse the re-parse set, then delete every old subtree it
        replaces; the pre-parsed trees and a re-run frontend's failure."""
        changed_entries = walk.scan.changed_entries
        deleted_before_parse = reparse.deleted_before_parse
        pre_parsed = self._file_parser.pre_parse_changed_files(changed_entries)
        self._flush_frontend_emission()
        # Every old subtree goes BEFORE any file of this run is parsed, not
        # one by one as each file is reached: a same-stem sibling parsed
        # earlier claims the survivor's old module qn, the MERGE lands on the
        # old node and rewrites its path, and the per-file delete by path
        # then finds nothing, leaving the old definitions beside the new
        # ones (issue #1569). A dependent re-parsed while a deleted file's
        # module is still in the graph would also resolve into it instead of
        # taking the fallback a clean index takes (issue #1584). This is the
        # only graph delete on the re-parse path: every non-new entry is in
        # reindexed_keys, so the per-file loop below only clears local
        # state. It runs only once every changed file has pre-parsed: a
        # parse failure then leaves the old subtrees in place instead of an
        # emptied graph.
        self._delete_stale_subtrees((*reparse.reindexed_keys, *deleted_before_parse))
        self._drop_deleted_file_state(deleted_before_parse)
        # A single-file run walked only its target, so it cannot supply the
        # other manifests; the helper walks for them.
        self._file_parser.resync_dependencies(
            (*reparsed_keys, *deleted_before_parse),
            reparsed_keys,
            walk.eligible_files if self._single_file is None else None,
        )
        # LIBCLANG ran before this method and emitted the covered files'
        # subtrees; the delete above matches by path, so it took any it had
        # just written for a stem-flux survivor, and the loop below skips
        # covered files, so nothing re-emits them (issue #2231). Run it again
        # now that the stale subtrees are gone, as reingest does before its
        # re-parse. HYBRID runs after Pass 2 and never meets this delete.
        #
        # An ADDED covered file goes first too. Its only subtree is what that
        # first pass wrote, and the pass MERGEd onto the Module a survivor
        # held under the same qn last run, rewriting its path to the added
        # file's: the survivor's old definitions then hang off a Module the
        # delete above, matching the survivor's path, cannot find.
        first_failure = self._rerun_flux_frontends(
            changed_entries,
            (*reparse.reindexed_keys, *deleted_before_parse, *reparse.added_keys),
        )
        return pre_parsed, first_failure

    def _parse_changed_files(
        self,
        walk: _Walk,
        pre_parsed: dict[Path, tuple[Node, dict[str, list] | None]],
        first_failure: Exception | None,
    ) -> tuple[int, Exception | None]:
        """Parse the re-parse set in walk order; how many files it took, and
        the first failure for the caller to raise before the cache commit."""
        scan = walk.scan
        changed_count = 0
        processed_since_flush = 0
        with Progress(
            SpinnerColumn(),
            TextColumn(ls.PROGRESS_INDEXING_LABEL),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
            disable=not sys.stderr.isatty(),
        ) as progress:
            task = progress.add_task("", total=len(walk.eligible_files))
            _advance_past_settled(progress, task, scan)

            for filepath, _file_key, is_new, file_bytes in scan.changed_entries:
                self._clear_state_before_reparse(filepath, is_new)

                changed_count += 1
                # Every stale subtree went in the loop above, so a failure
                # here must not abort this loop: every module after it would
                # stay deleted and never be rebuilt. The remaining files are
                # processed, the deletion reconciliation and inbound-edge
                # restore below still run, and the first error is re-raised
                # before the cache commit so the next run retries the lot.
                try:
                    self._file_parser.process_single_file(
                        filepath,
                        file_bytes=file_bytes,
                        pre_parsed=pre_parsed.get(filepath),
                    )
                except Exception as exc:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
                    logger.error(ls.INCREMENTAL_FILE_FAILED, path=filepath, error=exc)
                    if first_failure is None:
                        first_failure = exc

                processed_since_flush = self._flush_if_due(processed_since_flush + 1)

                progress.update(
                    task,
                    advance=1,
                    description=ls.PROGRESS_FILES_PROCESSED.format(count=changed_count),
                )
        return changed_count, first_failure

    def _flux_siblings(
        self,
        eligible_by_key: dict[str, Path],
        flux_stems: set[str],
        is_full_build: bool,
    ) -> set[str]:
        """Walked files beside a package flip or on a stem in flux."""
        # A directory whose package-ness flipped since the last run (an
        # __init__.py or Cargo.toml added or deleted) changes the container
        # every module directly under it hangs off; that edge is emitted only
        # when the module is parsed, so the siblings re-parse too. The stale
        # Package or Folder node itself is pruned at the end of the run
        # (issue #1570).
        flipped_dirs = self._package_flip_dirs() if not is_full_build else set()
        siblings = {
            key
            for key in eligible_by_key
            if (flipped_dirs and Path(key).parent.as_posix() in flipped_dirs)
            or (flux_stems and stem_key(key) in flux_stems)
        }
        return siblings

    def _queue_affected_callers(
        self,
        caller_keys: list[str],
        present: set[str],
        eligible_by_key: dict[str, Path],
        scan: FileScan,
        reindexed_keys: list[str],
    ) -> list[str]:
        """Queue each readable dependent for the re-parse; the re-indexed keys
        with the joined dependents counted in."""
        affected = 0
        for caller_key in caller_keys:
            if caller_key in present:
                continue
            caller_path = eligible_by_key.get(caller_key)
            if caller_path is None or not caller_path.is_file():
                continue
            try:
                caller_bytes = caller_path.read_bytes()
            except OSError:
                # An unchanged flux-stem survivor is first read here, after
                # the hash pass restored the unreadable ones' claims. Left
                # unparsed its subtree stays, so it keeps its claim as they do
                # and is marked for retry (PR #2248).
                #
                # The retry mark is owed whether or not a claim was forgotten:
                # an unopenable survivor keeps its seeded claim, and without
                # the mark its unchanged hash lets the next run skip it for
                # good (PR #2248).
                self._module_seeding.restore_unreadable_flux_claims({caller_key})
                scan.unreadable_keys.add(caller_key)
                if not vanished(caller_path):
                    scan.new_hashes[caller_key] = cs.HASH_CACHE_UNREADABLE
                continue
            caller_bytes = self._generated_sources.overlay.get(caller_key, caller_bytes)
            scan.changed_entries.append((caller_path, caller_key, False, caller_bytes))
            present.add(caller_key)
            affected += 1
        if affected:
            logger.info(ls.INCREMENTAL_AFFECTED_CALLERS, count=affected)
            reindexed_keys = _reindexed_keys(scan.changed_entries)
        return reindexed_keys

    def _queue_foreign_members(
        self,
        run: FilePassRun,
        reindexed_keys: list[str],
        deleted_keys: list[str],
        present: set[str],
        eligible_by_key: dict[str, Path],
        scan: FileScan,
    ) -> list[str]:
        """Queue the files that attach members to a module about to go.

        A Go method keys under its receiver type's module whatever file
        declares it, and a C++ out-of-line definition hangs on its header's
        class. The re-parse delete walks the module's subtree and takes them,
        and only their own file recreates them, so a type's file re-parsed as
        a mere dependent left `Box.Grow` (box_extra.go) deleted until
        box_extra.go itself changed (issue #3271). Read from the graph, so a
        fresh process finds them too; `foreign_definer_keys` reads this
        run's span records, which a new updater does not have yet. Repeated
        until nothing new joins: a joined file's own module can hold another
        file's members.
        """
        while True:
            joined = len(present)
            member_keys = self._dependent_lookup.foreign_member_keys(
                run, sorted({*reindexed_keys, *deleted_keys})
            )
            reindexed_keys = self._queue_affected_callers(
                member_keys,
                present,
                self._member_files(member_keys, eligible_by_key),
                scan,
                reindexed_keys,
            )
            if len(present) == joined:
                return reindexed_keys

    def _member_files(
        self, keys: list[str], eligible_by_key: dict[str, Path]
    ) -> dict[str, Path]:
        """Where each file attaching members is read from.

        A single-file run walked only its target, so the lookup's files are
        resolved on disk as `reingest` resolves them: looked up in the walk,
        each was dropped and the delete took its members for good (#3271).
        """
        if self._single_file is None:
            return eligible_by_key
        on_disk = {
            key: path
            for key in keys
            if (path := self._tree_scan.file_on_disk(key)) is not None
        }
        return {**on_disk, **eligible_by_key}

    def _captured_inbound_edges(
        self, run: FilePassRun, reindexed_keys: list[str], deleted_set: set[str]
    ) -> list[ResultRow]:
        """The re-indexed files' inbound edges, minus those from deleted files."""
        return [
            row
            for row in self._edge_carry.capture_inbound_edges(run, reindexed_keys)
            if row.get(cs.KEY_CALLER_PATH) not in deleted_set
        ]

    def _flush_frontend_emission(self) -> None:
        """Flush a frontend's buffered emission before the direct deletes."""
        # A frontend that ran before this method may still hold its emission
        # in the ingestor's buffer, and the deletes below go to the graph
        # directly: flushed, they see what the frontend wrote whenever the
        # buffer happened to fill, rather than sometimes the old subtree and
        # sometimes the new one (PR #2247).
        if self._frontend_runner.cpp_covered:
            self._ingestor.flush_all()

    def _delete_stale_subtrees(self, keys: Iterable[str]) -> None:
        """Delete the old subtree of every re-parsed or deleted file.

        A method of its own so the phase-order guard can pin it before the
        re-parse: `_delete_module_entities` also runs after the parse, for
        the late deletion pass, and a name-keyed guard cannot tell the two
        call sites apart (issue #1584).
        """
        for key in keys:
            self._delete_module_entities(key)

    def _drop_deleted_file_state(self, deleted_before_parse: list[str]) -> None:
        """Drop the in-memory state of every file deleted before the parse."""
        # The in-memory side of a deleted file goes now as well: a reused
        # updater (the watcher's `remove_file_from_state` path, or any caller
        # holding one across `run()` calls) otherwise resolves this
        # run's re-parsed importers against the deleted definitions and
        # suffixes a same-stem replacement's qn (issue #1575). The late
        # deletion block repeats this harmlessly.
        for deleted_key in deleted_before_parse:
            self._remover.remove_file_from_state(self._repo_path / deleted_key)

    def _rerun_flux_frontends(
        self,
        changed_entries: list[tuple[Path, str, bool, bytes]],
        flux_keys: tuple[str, ...],
    ) -> Exception | None:
        """Re-run the covering C++ frontend after the stale delete; its
        failure is returned for the caller to raise before the cache commit."""
        first_failure: Exception | None = None
        # Any covered file in flux re-runs it, an added one included.
        if settings.CPP_FRONTEND != cs.CppFrontend.HYBRID and any(
            key in self._frontend_runner.cpp_covered for key in flux_keys
        ):
            for _fp, file_key, is_new, _b in changed_entries:
                if is_new and file_key in self._frontend_runner.cpp_covered:
                    self._delete_module_entities(file_key)
            self._frontend_runner.cpp_covered = frozenset()
            # The stale subtrees are already gone, so a failing re-run must
            # not end the run here: with nothing covered the file pass below
            # rebuilds those files with tree-sitter, and the error is raised
            # before the cache commit so the next run retries, as a failed
            # file is (PR #2247). A failed run leaves nothing it
            # can vouch for.
            try:
                self._frontend_runner.run_cpp_frontend()
            except Exception as exc:  # noqa: BLE001 -- a frontend failing in any way costs only its facts
                logger.error(ls.INCREMENTAL_FRONTEND_RERUN_FAILED, error=exc)
                self._frontend_runner.cpp_covered = frozenset()
                first_failure = exc
        return first_failure

    def _clear_state_before_reparse(self, filepath: Path, is_new: bool) -> None:
        """Drop a known file's in-memory state before its re-parse."""
        if not is_new:
            self._remover.remove_file_from_state(
                filepath,
                frontend_current=bool(self._frontend_runner.cpp_covered)
                and cached_relative_path(filepath, self._repo_path).as_posix()
                in self._frontend_runner.cpp_covered,
            )

    def _flush_if_due(self, processed_since_flush: int) -> int:
        """Flush every `FILE_FLUSH_INTERVAL` files; the count since the last."""
        if processed_since_flush >= settings.FILE_FLUSH_INTERVAL:
            logger.info(ls.PERIODIC_FLUSH.format(count=processed_since_flush))
            self._ingestor.flush_all()
            processed_since_flush = 0
        return processed_since_flush

    def _delete_reconciled_files(
        self, deleted_keys: set[str], deleted_set: set[str]
    ) -> None:
        """Delete each reconciled file's state, subtree and File node."""
        graph = self._graph
        if deleted_keys:
            logger.info(ls.INCREMENTAL_DELETED, count=len(deleted_keys))
            for deleted_key in deleted_keys:
                deleted_path = self._repo_path / deleted_key
                # A key cleared before the parse gets neither a second state
                # drop nor a second module delete: the state drop keys on the
                # module qn, which a same-stem replacement parsed since has
                # taken over (its CommonJS export entry would go with it).
                # Only the File node is still to go.
                if deleted_key not in deleted_set:
                    self._remover.remove_file_from_state(deleted_path)
                    self._delete_module_entities(deleted_key)
                if graph is not None:
                    # Keyed on the absolute path: a sibling project's File
                    # node can share the relative path (issue #897). The
                    # entry's own identity, as the File was indexed: a file
                    # replaced by a link resolves to the link's target, whose
                    # File this deleted instead (#2451).
                    graph.execute_write(
                        cs.CYPHER_DELETE_FILE,
                        {cs.KEY_PATH: cached_file_identity_posix(deleted_path)},
                    )
