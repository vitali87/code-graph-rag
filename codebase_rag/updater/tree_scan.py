"""Walk the checkout and compare it with the hash cache the last run left: which
files changed, which were added or deleted, and the cache this run will
commit once its passes are durable."""

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Protocol

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..sync.state import (
    DirMtimesCache,
    FileHashCache,
    hash_file_with_bytes,
    load_hash_cache,
    touch_empty_json,
    trustworthy_cache_mtime,
)
from ..utils import path_utils
from ..utils.path_utils import (
    cached_relative_path,
    should_skip_path,
    vanished,
    walk_eligible_files,
)
from .generated_sources import GeneratedSources
from .sync_keeper import KeeperRun, SyncStateKeeper
from .types_defs import WalkScope


class TreeScanRun(KeeperRun, Protocol):
    """The sync run the scan is part of, which it marks a full build when
    nothing cached can be trusted to name what is already indexed."""

    is_full_build: bool


class HashBaseline(NamedTuple):
    """What the hash pass compares the walk against."""

    old_hashes: FileHashCache
    pristine_hashes: FileHashCache
    forced_reparse_keys: set[str]
    reindex_all: bool
    is_full_build: bool
    cache_mtime: float


@dataclass
class FileScan:
    """Per-run accumulator of the hash pass and its dependents."""

    new_hashes: FileHashCache = field(default_factory=dict)
    skipped_count: int = 0
    unreadable_count: int = 0
    unreadable_keys: set[str] = field(default_factory=set)
    current_file_keys: set[str] = field(default_factory=set)
    changed_entries: list[tuple[Path, str, bool, bytes]] = field(default_factory=list)
    single_gone_key: str | None = None

    def log_counts(self, changed_count: int) -> None:
        """Log how many files were skipped, changed and unreadable."""
        if self.skipped_count > 0:
            logger.info(ls.INCREMENTAL_SKIPPED, count=self.skipped_count)
        if changed_count > 0:
            logger.info(ls.INCREMENTAL_CHANGED, count=changed_count)
        if self.unreadable_count > 0:
            logger.info(ls.INCREMENTAL_UNREADABLE, count=self.unreadable_count)


def _log_skipped_symlinks(repo_path: Path, rel_paths: list[str]) -> None:
    # Each link at DEBUG and one count at INFO, so a module missing from the
    # graph can be traced to the link that was not followed (issue #2451).
    if not rel_paths:
        return
    root = repo_path.resolve()
    for rel_path in sorted(rel_paths):
        target = Path(os.path.realpath(repo_path / rel_path))
        if target.is_relative_to(root):
            inside = target.relative_to(root).as_posix()
            logger.debug(ls.SYMLINK_SKIPPED_INSIDE.format(path=rel_path, target=inside))
        else:
            logger.debug(
                ls.SYMLINK_SKIPPED_OUTSIDE.format(path=rel_path, target=target)
            )
    logger.info(ls.SYMLINKS_SKIPPED.format(count=len(rel_paths)))


class TreeScan:
    """The checkout as one run walks it, held against the hash cache.

    `single_file` marks a run that walked one file: it can update the
    cache entry of that file, but every project-wide term (the whole cache,
    every module the graph holds) would name its siblings as deleted, so
    those terms are scoped out. `existing_module_paths` reads the graph's
    module paths, the record of what is indexed when the cache cannot say.
    """

    def __init__(
        self,
        repo_path: Path,
        single_file: Path | None,
        scope: WalkScope,
        generated_sources: GeneratedSources,
        sync_keeper: SyncStateKeeper,
        existing_module_paths: Callable[[], frozenset[str] | None],
    ) -> None:
        self._repo_path = repo_path
        self._single_file = single_file
        self._scope = scope
        self._generated_sources = generated_sources
        self._sync_keeper = sync_keeper
        self._existing_module_paths = existing_module_paths
        # The directory mtimes the last walk saw, stashed with the cache.
        self.collected_dir_mtimes: DirMtimesCache = {}

    def ignores(self, path: Path) -> bool:
        # The caller is an agent naming files it wrote, so the project's
        # ignore rules apply here exactly as they do to the walk: a path
        # under `node_modules`, a generated `.min.js`, or a CLI-excluded
        # directory must not become a Module the next update then keeps,
        # because its hash matches and the walk never revisits it.
        return should_skip_path(
            path,
            self._repo_path,
            exclude_paths=self._scope.exclude_paths,
            unignore_paths=self._scope.unignore_paths,
            is_file=True,
        )

    def file_on_disk(self, key: str) -> Path | None:
        """The file at `key` when the walk would index it, else None."""
        candidate = self._repo_path / key
        if (
            not candidate.is_file()
            or path_utils.is_symlink_entry(candidate)
            or self.ignores(candidate)
        ):
            return None
        return candidate

    def _single_target_vanished(self, filepath: Path) -> bool:
        """Whether `filepath` is this run's single target and its entry is gone.

        Judged by the MISSING-PATH error from `lstat`, not by `Path.exists()`:
        `exists()` answers False to any metadata failure, so a target that is
        merely unreadable would have been classified as deleted and lose its
        state and cache entry (#1755). `lstat` needs only search
        permission on the directory, and `FileNotFoundError` from it means the
        directory entry itself is gone.
        """
        if self._single_file is None or filepath != self._single_file:
            return False
        try:
            os.lstat(filepath)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return False

    def collect_eligible_files(self) -> list[tuple[Path, str]]:
        if self._single_file is not None:
            if not should_skip_path(
                self._single_file,
                self._repo_path,
                exclude_paths=self._scope.exclude_paths,
                unignore_paths=self._scope.unignore_paths,
            ):
                file_key = cached_relative_path(
                    self._single_file, self._repo_path
                ).as_posix()
                return [(self._single_file, file_key)]
            return []

        eligible: list[tuple[Path, str]] = []
        self.collected_dir_mtimes = {}
        # One shared walk with the C++ module-qn map, so the two cannot drift
        # in ORDER as well as in membership (issues #1025, #1099).
        skipped_links: list[str] = []
        for dirpath, fname, rel_path_str in walk_eligible_files(
            self._repo_path,
            self._scope.exclude_paths,
            self._scope.unignore_paths,
            on_dir=self._record_dir_mtime,
            on_symlink=skipped_links.append,
        ):
            eligible.append((Path(f"{dirpath}/{fname}"), rel_path_str))
        _log_skipped_symlinks(self._repo_path, skipped_links)
        return eligible

    def _record_dir_mtime(self, dir_key: str, dirpath: str) -> None:
        try:
            self.collected_dir_mtimes[dir_key] = os.stat(dirpath).st_mtime
        except OSError as e:
            # The walk has just listed it, so only a directory removed since
            # fails here. Left out of the record, it is a directory the next
            # in-sync check does not expect, and its files, gone from disk,
            # are not hashed either.
            logger.debug(ls.DIR_MTIME_UNREADABLE, path=dir_key, error=e)

    def _load_old_hashes(
        self, run: TreeScanRun, state_dir: Path, force: bool, cache_path: Path
    ) -> tuple[FileHashCache, FileHashCache, set[str], bool]:
        """The previous run's hashes, the single-file snapshot, the keys
        forced through a re-parse, and whether every cached file is re-indexed."""
        # `_cache_discarded_in_memory`: the orphan discard could not delete the
        # file, so treat it as gone for this run (issue #1647). Without this the
        # mtime fast path below skips every file against hashes known dead, and
        # the run indexes nothing on every subsequent run too.
        cache_is_dead = run.cache_discarded_in_memory
        # A repository cache can contain hashes from another project. Reusing
        # those hashes would skip every unchanged file even though this
        # project has not been parsed into the graph. Exclusion-set changes
        # are different: the old cache is needed to identify files that just
        # became excluded, so keep it when project provenance still matches.
        self._sync_keeper.exclusions_match_last_run(run, state_dir)
        old_hashes = (
            load_hash_cache(cache_path)
            if not (force or cache_is_dead or run.cache_project_match is False)
            else {}
        )
        # Snapshot for the single-file merge, read from DISK and independently
        # of `force`. Two distinct reasons it cannot reuse `old_hashes`:
        #
        # 1. the stale-key pop below -- a single-file run does not commit the
        #    delombok state (see `run`), so it must not act on that pop
        #    either. Merging over the popped dict would DROP every
        #    Lombok-affected sibling, which then takes the `is_new` path next
        #    run and skips delete-before-reingest, leaving the previous
        #    parse's entities beside the fresh ones (#1619).
        # 2. `force` -- which empties `old_hashes` to make this run RE-PARSE
        #    what it walked. A single-file run walked one file, so `force`
        #    says nothing about the siblings it never looked at. Reusing the
        #    emptied dict would persist a cache holding only the target and
        #    erase every sibling's hash, which is the same data loss the
        #    unforced path already guards (#1619).
        #
        # Only the single-file commit reads this; a project run replaces the
        # cache wholesale from its own complete walk, as it always has.
        # A discarded cache is dead for the snapshot too: merging over it
        # would carry forward the very hashes the discard rejected.
        # A cache stamped by ANOTHER project holds that project's hashes:
        # merging this project's file hash into it and publishing would tell
        # the other project's next sync the file is current while its graph
        # still holds the old parse (PR #2251). Treated like a
        # missing cache, so nothing is published.
        pristine_hashes = (
            load_hash_cache(cache_path)
            if self._single_file is not None
            and not cache_is_dead
            and run.cache_project_match is not False
            else {}
        )
        # Remembered BEFORE the pop: a key forced through re-parse by a
        # Delombok overlay change was indexed by the previous run, and
        # popping it from `old_hashes` is what forces the re-hash. It must
        # not also make the file look NEW to the classification below --
        # `is_new` skips the delete-before-reingest, so the file's previous
        # subtree stayed in the graph beside the fresh parse whenever the
        # graph-backed `preexisting_paths` had nothing to say (issue #1657).
        stale_keys = self._generated_sources.stale_keys
        forced_reparse_keys = {key for key in stale_keys if key in old_hashes}
        for stale_key in stale_keys:
            old_hashes.pop(stale_key, None)
        # `_parser_changed`: the cache names files the old inputs parsed, so
        # every one of them is forced through a re-parse the same way an
        # overlay change forces its files (issue #1977). Forced, not merely
        # forgotten: a file dropped from `old_hashes` alone reads as NEW,
        # skips delete-before-reingest and keeps its previous registry
        # entries, so a reused updater registers each definition again as
        # an `@N` duplicate (the delombok overlay test caught it in CI).
        # Only when the cache named something: a cache file that loads empty
        # (corrupt JSON, or one this run already discarded) leaves nothing to
        # force, and that run stays the full build it always was, asking the
        # graph what it holds.
        reindex_all = run.parser_changed and bool(old_hashes)
        if reindex_all:
            forced_reparse_keys |= set(old_hashes)
            old_hashes.clear()
        return old_hashes, pristine_hashes, forced_reparse_keys, reindex_all

    def hash_baseline(
        self, run: TreeScanRun, state_dir: Path, force: bool
    ) -> HashBaseline:
        """Everything the hash pass compares against, read before the walk."""
        cache_path = state_dir / cs.HASH_CACHE_FILENAME
        old_hashes, pristine_hashes, forced_reparse_keys, reindex_all = (
            self._load_old_hashes(run, state_dir, force, cache_path)
        )
        # That run is a RE-INDEX of an indexed tree, not a first build.
        is_full_build = (
            force or (not old_hashes and not reindex_all)
        ) and self._single_file is None
        run.is_full_build = is_full_build
        cache_mtime = (
            trustworthy_cache_mtime(cache_path) if cache_path.is_file() else 0.0
        )
        if force:
            logger.info(ls.INCREMENTAL_FORCE)

        # Same rule as the publish site in `run`: a single-file run may
        # UPDATE an existing cache but must never CREATE one. These two
        # calls are what actually bring a stray into existence -- they run
        # before any publishing -- so the guard has to be here as well.
        #
        # No `or cache_path.is_file()` arm: `touch_empty_json` already
        # returns early when the file exists, so the update case is
        # unaffected either way and the extra arm could never fail. Its
        # absence is checked by the "still updates an existing cache" test,
        # which reddens on the publish-site guard rather than this one.
        if self._single_file is None:
            touch_empty_json(cache_path)
            touch_empty_json(state_dir / cs.DIR_MTIMES_FILENAME)
        return HashBaseline(
            old_hashes=old_hashes,
            pristine_hashes=pristine_hashes,
            forced_reparse_keys=forced_reparse_keys,
            reindex_all=reindex_all,
            is_full_build=is_full_build,
            cache_mtime=cache_mtime,
        )

    def preexisting_paths(
        self, run: TreeScanRun, state_dir: Path, force: bool, baseline: HashBaseline
    ) -> frozenset[str] | None:
        """The graph's module paths, on the runs whose cache cannot name them."""
        old_hashes = baseline.old_hashes
        is_full_build = baseline.is_full_build
        reindex_all = baseline.reindex_all
        # A full build can still land on a graph that already holds this
        # project: the cache lives in the repo working tree, the graph does
        # not, so a fresh clone of an indexed repo (or a force run) parses
        # every file as "new" while the previous parse's state is still
        # there. A file whose Module already exists must take the
        # delete-before-reingest path or renamed-away symbols and their
        # CALLS/REFERENCES edges accumulate alongside the fresh parse.
        # A changed exclusion set has to reconcile against the GRAPH, not just
        # against the hash cache. Since #1615 the cache commits only after the
        # flush, so a crashed run cannot leave one that has already forgotten
        # the file. The graph query is still required for a different reason:
        # the cache records only the files eligible on the run that wrote it,
        # so a file excluded by an EARLIER completed run was already dropped
        # from it and no on-disk hash names it any more. Only the graph still
        # does. It costs a query only on the runs where the set actually
        # moved.
        # A single-file run whose cache cannot name the target needs this too,
        # for the same reason a full build does. `is_new` would otherwise fall
        # through to True on a file the previous parse already indexed, skip
        # delete-before-reingest, and stack a second parse's entities on the
        # first -- renamed-away symbols and their CALLS/REFERENCES edges
        # surviving beside the fresh ones (#1619).
        #
        # The condition mirrors `is_full_build` above deliberately: `force`
        # empties `old_hashes`, and a missing or evicted cache leaves it empty
        # the same way, so both must ask the graph. Guarding on `force` alone
        # left the identical defect on a fresh clone of an already-indexed
        # repo -- the cache lives in the working tree, the graph does not --
        # and after any cache eviction (#1619).
        cacheless_single_file = (force or not old_hashes) and (
            self._single_file is not None
        )
        # A parser-changed re-index asks too: the forced set names only what
        # the cache named, and a file the graph holds but the cache omits
        # would otherwise read as new and skip delete-before-reingest (bot
        # review).
        preexisting_paths = (
            self._existing_module_paths()
            if is_full_build
            or reindex_all
            or cacheless_single_file
            or not self._sync_keeper.exclusions_match_last_run(run, state_dir)
            else frozenset()
        )
        # None is "the sink claims readability and the query failed", not
        # "nothing there": below it falls back to the hash cache alone, which
        # cannot name a file whose exclusion predates it. That run completes
        # and would stamp, and every later run would fast-path over the
        # surviving subtree. Remembering the failure keeps the stamp back.
        if preexisting_paths is None:
            run.graph_state_unknown = True
        return preexisting_paths

    def single_target_gone_before_scan(
        self, eligible_files: list[tuple[Path, str]]
    ) -> str | None:
        """The single-file target's key when it vanished before the hash pass."""
        if (
            self._single_file is not None
            and eligible_files
            and not self._single_file.exists()
        ):
            return eligible_files[0][1]
        return None

    def _scan_mtime_fast_path(
        self, filepath: Path, file_key: str, baseline: HashBaseline, scan: FileScan
    ) -> bool:
        """Settle a cached file without hashing it; True when it was settled."""
        try:
            file_mtime = filepath.stat().st_mtime
        except OSError as stat_error:
            # The stat's own error decides: a missing path is a
            # deletion, any other failure an unreadable file.
            if isinstance(
                stat_error, FileNotFoundError
            ) and self._single_target_vanished(filepath):
                scan.single_gone_key = file_key
                return True
            scan.unreadable_count += 1
            scan.unreadable_keys.add(file_key)
            # Marked for retry unless the path is GONE (vanished, or
            # a broken symlink): those would refuse the fast path on
            # every later run. A file that exists but cannot be
            # reached (a permission failure on it or its directory)
            # is marked; `exists()` alone reads such a file as gone
            # .
            if not vanished(filepath):
                scan.new_hashes[file_key] = cs.HASH_CACHE_UNREADABLE
            return True
        # A file marked unreadable by the previous run is hashed
        # whatever its mtime: the mark is not a digest, so the
        # comparison below sees a change and the file is re-parsed
        # as a KNOWN file, its old subtree deleted first (#1983).
        if (
            file_mtime <= baseline.cache_mtime
            and baseline.old_hashes[file_key] != cs.HASH_CACHE_UNREADABLE
        ):
            scan.new_hashes[file_key] = baseline.old_hashes[file_key]
            scan.current_file_keys.add(file_key)
            scan.skipped_count += 1
            return True
        return False

    def scan_eligible_file(
        self,
        filepath: Path,
        file_key: str,
        force: bool,
        baseline: HashBaseline,
        preexisting_paths: frozenset[str] | None,
        scan: FileScan,
    ) -> None:
        """Hash one walked file and queue it for the parse when it changed."""
        old_hashes = baseline.old_hashes
        if (
            not force
            and file_key in old_hashes
            and self._scan_mtime_fast_path(filepath, file_key, baseline, scan)
        ):
            return

        hashed = hash_file_with_bytes(filepath)
        if hashed is None:
            # The same deletion, one step later: a target that passed the
            # existence check and was removed before its stat or read is
            # gone all the same, and the unreadable path would keep its
            # state and cache entry (#1755).
            if self._single_target_vanished(filepath):
                scan.single_gone_key = file_key
                return
            scan.unreadable_count += 1
            scan.unreadable_keys.add(file_key)
            if not vanished(filepath):
                scan.new_hashes[file_key] = cs.HASH_CACHE_UNREADABLE
            return
        current_hash, file_bytes = hashed
        # The hash keys the CHECKED-IN source (cache invalidation follows
        # edits); the PARSE may consume the delomboked expansion instead,
        # so Lombok-generated members become real graph nodes (#1140).
        file_bytes = self._generated_sources.overlay.get(file_key, file_bytes)

        scan.current_file_keys.add(file_key)
        scan.new_hashes[file_key] = current_hash

        if (
            not force
            and file_key in old_hashes
            and old_hashes[file_key] == current_hash
        ):
            logger.debug(ls.FILE_HASH_UNCHANGED, path=file_key)
            scan.skipped_count += 1
            return

        is_new = (
            file_key not in old_hashes
            and file_key not in baseline.forced_reparse_keys
            and preexisting_paths is not None
            and file_key not in preexisting_paths
        )
        if not is_new:
            logger.debug(ls.FILE_HASH_CHANGED, path=file_key)
        else:
            logger.debug(ls.FILE_HASH_NEW, path=file_key)
        scan.changed_entries.append((filepath, file_key, is_new, file_bytes))

    def keys_deleted_before_parse(
        self,
        baseline: HashBaseline,
        preexisting_paths: frozenset[str] | None,
        eligible_by_key: dict[str, Path],
        unreadable_keys: set[str],
    ) -> list[str]:
        """The files whose subtrees go before the parse: gone from the walk."""
        old_hashes = baseline.old_hashes
        forced_reparse_keys = baseline.forced_reparse_keys
        # A dependent of a DELETED file is in the same position: its edges
        # into that file go with the subtree and cannot be restored, and a
        # clean index would re-derive them from what remains (a phantom
        # external parent, a fallback, or nothing). The deleted set is what
        # the cache remembers and the walk no longer finds; it joins the
        # dependents query now, while the subtrees still exist to match
        # against (issue #1567).
        # `old_hashes` has already had the Delombok-stale keys popped (so a
        # changed overlay forces those files through a reparse), which would
        # otherwise hide a file that is BOTH stale and deleted: it would be
        # absent from the cache set here and never join the dependents query.
        # Union them back in; the walk-eligible subtraction below drops the
        # ones that still exist (issue #1567).
        # A file the graph still holds but neither the cache nor the walk
        # names (a cacheless or forced rebuild over an existing graph) must
        # go before the parse too, for the same-stem reason as above: a
        # surviving sibling parsed first would otherwise claim its qn and
        # the post-parse delete by path would find nothing.
        graph_only_gone = (
            preexisting_paths - eligible_by_key.keys() - unreadable_keys
            if preexisting_paths
            else frozenset()
        )
        # Empty on a single-file run, for the same reason `deleted_keys`
        # below is: every term here is PROJECT-WIDE (the whole hash cache, the
        # whole Delombok-stale set, every module the graph holds), and the
        # only thing subtracted is `eligible_by_key` -- which on a single-file
        # run names just the walked file. So each sibling falls straight
        # through and is deleted before the parse, which is the sweep #1619
        # exists to stop. It was previously bounded only by accident, because
        # nothing on this path populated the set (issue #1671); that is not a
        # property to rely on, so the scope rule is stated here.
        deleted_before_parse = (
            sorted(
                (
                    set(old_hashes)
                    | self._generated_sources.stale_keys
                    | forced_reparse_keys
                    | graph_only_gone
                )
                - eligible_by_key.keys()
                - unreadable_keys
            )
            if self._single_file is None
            else []
        )
        return deleted_before_parse

    def deleted_keys_for_run(
        self,
        old_hashes: FileHashCache,
        preexisting_paths: frozenset[str] | None,
        scan: FileScan,
    ) -> set[str]:
        """The files this run reconciles away after the parse."""
        current_file_keys = scan.current_file_keys
        unreadable_keys = scan.unreadable_keys
        single_gone_key = scan.single_gone_key
        # On a cacheless rebuild old_hashes cannot name what disappeared, so
        # the graph's own module paths join the reconciliation: a file that
        # was deleted OR newly excluded since the previous index is absent
        # from current_file_keys and its subtree must go. (Disk-deleted files
        # are also swept by orphan pruning; excluded ones still exist on disk
        # and only this reconciliation catches them.) A file that merely
        # could not be READ this run still exists: sweeping it would erase
        # live state over a transient error, so unreadable keys are exempt.
        graph_paths = (
            preexisting_paths if preexisting_paths is not None else frozenset()
        )
        # A single-file run walked exactly one file, so `current_file_keys`
        # names only that file and every sibling looks deleted. It cannot
        # speak for the project's file set, for the same reason `run` guards
        # the exclusion stamp with `self._single_file is None` (#1619).
        if self._single_file is None:
            deleted_keys = (
                (set(old_hashes.keys()) | graph_paths)
                - current_file_keys
                - unreadable_keys
            )
        elif single_gone_key is not None:
            # The one file this run was asked about is gone: exactly that key,
            # never the siblings the project-wide terms would name (#1737).
            deleted_keys = {single_gone_key}
        else:
            deleted_keys = set()
        return deleted_keys

    def stash_pending_caches(
        self,
        run: TreeScanRun,
        state_dir: Path,
        scan: FileScan,
        baseline: HashBaseline,
    ) -> None:
        """Stash the hash cache and directory mtimes for `run` to commit."""
        cache_path = state_dir / cs.HASH_CACHE_FILENAME
        new_hashes = scan.new_hashes
        unreadable_keys = scan.unreadable_keys
        single_gone_key = scan.single_gone_key
        pristine_hashes = baseline.pristine_hashes
        cache_mtime = baseline.cache_mtime
        if self._single_file is None:
            run.pending_hash_cache = (cache_path, new_hashes)
            # A file this run could not read carries the unreadable mark in
            # the cache: the next run's in-sync check refuses on it and the
            # pass hashes it whatever its mtime, so it is retried as a KNOWN
            # file with its old subtree deleted first (issue #1983).
            if unreadable_keys:
                logger.warning(
                    ls.INCREMENTAL_UNREADABLE_RETRY, count=len(unreadable_keys)
                )
            run.pending_dir_mtimes = (
                state_dir / cs.DIR_MTIMES_FILENAME,
                self.collected_dir_mtimes,
            )
        else:
            # Two different remedies, because the run has different standing
            # on each (#1619). It DID hash its one file, so that hash is worth
            # recording -- merged over the previous run's entries, never
            # replacing them, or the next full run treats every sibling as
            # new. It collected NO directory mtimes at all
            # (`collect_eligible_files` returns above the walk that populates
            # them), so there is nothing to merge and writing the empty
            # collection would wipe the previous walk's record wholesale.
            # ...but only when there IS a previous run's cache to merge over.
            # With none (missing, empty or unreadable), publishing the one
            # hash this run took would create a cache naming a single file,
            # which the next PROJECT run reads as authoritative: every
            # sibling is then absent from `old_hashes`, takes the `is_new`
            # path, skips delete-before-reingest, and an edited sibling's
            # previous entities survive beside the fresh parse. Writing
            # nothing keeps the next project run a full build, which
            # reconciles properly (#1619). Measured: with the
            # cache removed, a single-file run then a project run left
            # `module_b.py` undeleted after an edit.
            merged_hashes = {**pristine_hashes, **new_hashes}
            if single_gone_key is not None:
                # The deleted target must leave the cache too, or the next
                # project run reads it as indexed and never reconciles the
                # graph (#1737).
                merged_hashes.pop(single_gone_key, None)
            run.pending_hash_cache = (
                (cache_path, merged_hashes) if pristine_hashes else None
            )
            # And it must carry the PREVIOUS cache's mtime forward rather
            # than stamping this run's instant. The merged entries are mostly
            # the previous run's hashes, so stamping now would assert every
            # sibling was observed just now: a sibling edited before this run
            # then satisfies `file_mtime <= cache_mtime` with a hash that
            # still matches, and the next project run reports "already in
            # sync" and never indexes the edit (#1619).
            #
            # Note this must be the previous mtime, NOT None. None skips the
            # `os.utime` in `run` altogether, which leaves the file its own
            # write time -- LATER than this run's instant, so it makes the
            # trap worse rather than closing it. `cache_mtime` was read at the
            # top of this method, before anything wrote to the cache.
            #
            # `or 0.0`, never `or None`: `cache_mtime` is 0.0 when the previous
            # stamp was distrusted for being in the future (issue #1665), and
            # 0.0 is falsy, so `or None` would take exactly the branch the
            # paragraph above warns against. The epoch vouches for nothing,
            # which is the honest claim for a run whose predecessor's stamp
            # could not be trusted; None vouches for now, and loses a sibling
            # edited before this run, permanently.
            run.pending_cache_observed_at = cache_mtime or 0.0

    def stash_parser_fingerprint(
        self, run: TreeScanRun, state_dir: Path, scan: FileScan, is_full_build: bool
    ) -> None:
        """Stash the parser fingerprint when this run covered the project."""
        unreadable_keys = scan.unreadable_keys
        new_hashes = scan.new_hashes
        unmarked_unreadable = {
            key
            for key in unreadable_keys
            if new_hashes.get(key) != cs.HASH_CACHE_UNREADABLE
        }
        covered_the_project = not unmarked_unreadable and not run.graph_state_unknown
        if covered_the_project and (
            is_full_build or (run.parser_changed and self._single_file is None)
        ):
            run.pending_parser_fingerprint = self._sync_keeper.parser_fingerprint(
                state_dir
            )
