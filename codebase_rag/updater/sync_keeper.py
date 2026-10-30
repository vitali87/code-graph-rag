"""Keep the sync state that lets a run skip what the last one already did: the
checks a cached run must pass before it is trusted, and the commit that
records a finished run's hash cache, directory mtimes and stamps."""

import os
import stat
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from loguru import logger

from .. import constants as cs
from .. import cypher_queries as cq
from .. import logs as ls
from ..capture import CaptureSelection
from ..parser_fingerprint import compute_parser_fingerprint
from ..services import IngestorProtocol, QueryProtocol
from ..sync.state import (
    DirMtimesCache,
    FileHashCache,
    exclusion_scope,
    exclusion_state,
    hash_file,
    is_placeholder_hash_cache,
    load_dir_mtimes,
    load_exclusion_state,
    load_hash_cache,
    load_parser_fingerprint,
    load_project_stamps,
    publish_hash_cache,
    save_dir_mtimes,
    save_exclusion_state,
    save_parser_fingerprint,
    trustworthy_cache_mtime,
)
from ..types_defs import PropertyParams, ResultRow
from ..utils.path_utils import should_keep_dir, should_skip_rel_file
from ..vectors.embedding_sync import EmbeddingSync
from .generated_sources import GeneratedSources
from .types_defs import WalkScope


class KeeperRun(Protocol):
    """The sync state a run learns before its passes and stashes for the
    commit, which writes it only once every pass and flush succeeded
    (issues #1615, #1634): a build that dies before its final flush must
    not leave state claiming it finished."""

    cache_discarded_in_memory: bool
    parser_changed: bool
    exclusion_match: bool | None
    cache_project_match: bool | None
    graph_state_unknown: bool
    exposes_cleanup_skipped: bool
    pending_hash_cache: tuple[Path, FileHashCache] | None
    pending_dir_mtimes: tuple[Path, DirMtimesCache] | None
    pending_cache_observed_at: float | None
    pending_parser_fingerprint: str | None


def _direct_children(keys: Iterable[str], prefix: str) -> set[str]:
    children: set[str] = set()
    for key in keys:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        if "/" not in rest:
            children.add(rest)
    return children


def _entry_flag(probe: Callable[[], bool]) -> bool:
    try:
        return probe()
    except OSError:
        return False


def _scan_dir_children(dir_path_str: str) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    dirs: set[str] = set()
    with os.scandir(dir_path_str) as it:
        for entry in it:
            name = entry.name
            # The walk follows no link, file or directory (issue #2451), so
            # neither may the listing compared against its cache: a file link
            # here would read as a new file and refuse the fast path forever.
            if name in cs.CGR_STATE_FILENAMES or _entry_flag(entry.is_symlink):
                continue
            (dirs if _entry_flag(entry.is_dir) else files).add(name)
    return files, dirs


def _cached_file_unchanged(
    file_path_str: str, old_hash: str, cache_mtime: float
) -> bool:
    if old_hash == cs.HASH_CACHE_UNREADABLE:
        # A file the last run could not read is retried by the next real
        # pass, whatever its directory's mtime says (#1983).
        return False
    try:
        info = os.lstat(file_path_str)
    except OSError:
        return False
    # A cached path that is a link now, or was indexed through one before
    # links stopped being followed (issue #2451), is not walked, so it is not
    # unchanged: the sync must drop it. `lstat` costs what `stat` did, and
    # equals it for every other file.
    if stat.S_ISLNK(info.st_mode):
        return False
    if info.st_mtime <= cache_mtime:
        return True
    try:
        current_hash = hash_file(Path(file_path_str))
    except OSError:
        # A cached file that can no longer be read is not "in sync": the
        # batch pass counts it unreadable and leaves it out of the cache,
        # where raising here ended the run (issue #1992).
        return False
    return current_hash == old_hash


def _cached_files_unchanged(
    repo_str: str, old_hashes: FileHashCache, cache_mtime: float
) -> bool:
    return all(
        _cached_file_unchanged(f"{repo_str}/{file_key}", old_hash, cache_mtime)
        for file_key, old_hash in old_hashes.items()
    )


class SyncStateKeeper:
    """The sync state of one checkout, as one project's runs read and write it.

    The hash cache and directory mtimes are repository-scoped while the
    graph they vouch for is project-scoped, so every check that lets a run
    trust them also asks whether this project, under this scope, wrote
    them. `single_file` marks a run that walked one file, which can update
    that state but never speak for the whole tree.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        graph_rows: Callable[[str, PropertyParams | None], list[ResultRow]],
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        project_named: bool,
        capture: CaptureSelection,
        single_file: Path | None,
        scope: WalkScope,
        embedding_sync: EmbeddingSync,
        generated_sources: GeneratedSources,
    ) -> None:
        # `graph` is the store as a readable graph, None for a write-only
        # sink; `ingestor` is the raw store, whose own `fetch_all` the
        # orphaned-cache check asks.
        self._graph = graph
        self._graph_rows = graph_rows
        self._ingestor = ingestor
        self._repo_path = repo_path
        self._project_name = project_name
        self._project_named = project_named
        self._capture = capture
        self._single_file = single_file
        self._scope = scope
        self._embedding_sync = embedding_sync
        self._generated_sources = generated_sources

    def record_sync_time(self) -> None:
        """Stamp the Project with when this sync completed (issue #2444).

        Every whole-project sync commits in `_commit_run_state` or
        `_finish_in_sync_run`, the CLI's, MCP's and the watcher's alike, and
        stamps there, so `cgr status` reads the time from the graph it
        reports on instead of a log that outlived deleted projects and mixed
        in other graphs'. Part of the commit, so a Ctrl+C that lands after it
        still finds the sync recorded (#2442). A single-file run brings one
        file up to date, not the project, and a write-only sink has no graph
        to stamp. Best effort: the sync is complete and durable by now.
        """
        graph = self._graph
        if self._single_file is not None or graph is None:
            return
        try:
            graph.execute_write(
                cq.CYPHER_RECORD_PROJECT_SYNC,
                {
                    cs.KEY_PROJECT_NAME: self._project_name,
                    cs.KEY_LAST_SYNCED_AT: datetime.now(UTC).isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the write is retried later
            logger.warning(
                ls.SYNC_TIME_NOT_RECORDED.format(project=self._project_name, error=exc)
            )

    def commit_caches(self, run: KeeperRun, state_dir: Path) -> None:
        """Write the parser fingerprint, hash cache, directory mtimes and
        exclusion stamp this run stashed for its commit point."""
        # The parser fingerprint commits here too, for the same reason
        # (issue #1634). `reparse_all_if_parser_changed` compares the stored stamp
        # with the current fingerprint to tell the user the graph was built by
        # different parser code; a stamp written inside `_process_files`, before
        # the flushes above, survived a full build that died in between and
        # claimed this parser's edges were in a graph that held none of them,
        # so the very run that failed was the one the warning stayed silent
        # for. Set by a full build or a parser-changed re-index
        # (`_process_files`), so ordinary incremental runs keep the stale
        # stamp and the warning with it.
        if run.pending_parser_fingerprint is not None:
            save_parser_fingerprint(
                state_dir / cs.PARSER_FINGERPRINT_FILENAME,
                run.pending_parser_fingerprint,
            )
            run.pending_parser_fingerprint = None

        # Same commit point, for the same reason. The module deletions a newly
        # excluded file triggers are execute_write calls that only become
        # durable at the flush above; stamping back in _process_files would let
        # a run that died in between tell its successor the exclusion was
        # already reconciled, and the successor would fast-path over a subtree
        # still in the graph. Deferring the stamp buys only that: the successor
        # RUNS. It does not on its own make the successor delete anything:
        # the cache commits below, after this flush (issue #1615), so a run
        # that died earlier leaves no cache at all and its successor re-hashes
        # from scratch. What names a file whose exclusion predates the cache
        # is the graph-backed reconciliation in _process_files, which the
        # changed exclusion set turns on; the two are needed together.
        # Recorded on EVERY completed project run, not full builds only: an
        # incremental run that narrowed the set must record it, or the next run
        # reads that run's own result as a change. A single-file run walks one
        # file and cannot speak for the project's exclusion set.
        # Committed here, not in `_process_files` (issue #1615), so a run that
        # dies before the flush leaves a cache that still names the deleted
        # file and its successor re-issues the delete. Deliberately NOT inside
        # the single-file guard below: a single-file run writes the cache today
        # and must keep doing so. Written even when `_graph_state_unknown` is
        # set, because the cache only claims which files were parsed at which
        # hashes, which stays true; the withheld exclusion stamp is what tells
        # the next run that deletions were not reconciled, and that alone both
        # fails the sync check and turns graph-backed reconciliation on.
        self._commit_hash_cache(run)

        if self._single_file is None:
            if run.graph_state_unknown:
                logger.warning(ls.EXCLUSION_STATE_NOT_RECORDED)
            else:
                self.stamp_exclusion_state(state_dir)

    def _commit_hash_cache(self, run: KeeperRun) -> None:
        observed_at = run.pending_cache_observed_at
        # Hash cache FIRST, directory mtimes LAST, because a stop between the
        # two leaves a mismatched pair that is only detected in one direction.
        # `_is_already_in_sync` iterates the entries the map RECORDS, so it
        # never reaches a directory the map has no entry for. What catches an
        # addition is the recorded entry for the directory that DIRECTLY
        # CONTAINS it, whose stored mtime no longer matches disk: that sends
        # `_diff_dir_against_cache` into it to find the new child. For a new
        # subdirectory that containing entry is the parent; for a file added
        # to a directory already in the map it is that directory's own entry,
        # and the parent contributes nothing. Measured both shapes by
        # refreshing one entry at a time: refreshing the containing entry
        # alone hides the addition, refreshing any other leaves it visible.
        # Checked on all three shapes: a new subdirectory, a file added to a
        # directory already recorded, and a file added at the repo root.
        # A stale map holds every entry's old mtime, so the addition surfaces
        # either way. A FRESH map records them all as current, nothing is
        # compared, and the file loop only walks keys the hash cache already
        # names, so a file added in the gap is never indexed. Publishing the
        # hash cache first leaves the harmless window.
        # A failed publish leaves the PREVIOUS hash cache on disk, so advancing
        # the map anyway would build exactly the fresh-map/stale-cache pair the
        # ordering above exists to avoid: nothing would be compared, and a file
        # added during the run would never be indexed. Withholding the map
        # keeps both artefacts one run behind, which only costs a rescan.
        hash_cache_published = True
        if run.pending_hash_cache is not None:
            cache_path, new_hashes = run.pending_hash_cache
            # A single-file run may UPDATE an existing cache but must never
            # CREATE one, for the same reason it does not record the
            # exclusion state below: it walked one file and cannot describe
            # a directory's contents.
            #
            # Creating one is actively harmful since #1775, because the
            # cache is what marks a project root. A cache created under
            # `pkg/` by a run targeting `pkg/module_a.py` makes every later
            # single-file run below `pkg/` root THERE, which preserves
            # exactly the misrooting #1775 removes. Measured in review: the
            # stray held all three sibling files, so it is indistinguishable
            # by content from a genuine project's cache and cannot be
            # filtered out after the fact -- it has to not be written.
            #
            # Updating an existing one stays correct: such a run is rooted
            # at the ancestor that owns that cache, so the entries it writes
            # are keyed against the same root every other run uses.
            if self._single_file is not None and not cache_path.is_file():
                logger.info(ls.HASH_CACHE_SKIPPED_SINGLE_FILE, path=cache_path)
            else:
                hash_cache_published = publish_hash_cache(
                    cache_path, new_hashes, observed_at
                )
            run.pending_hash_cache = None
        if run.pending_dir_mtimes is not None and hash_cache_published:
            save_dir_mtimes(*run.pending_dir_mtimes)
        run.pending_dir_mtimes = None
        run.pending_cache_observed_at = None

    def stamp_exclusion_state(
        self, state_dir: Path, *, only_if_changed: bool = False
    ) -> None:
        """Record this run's scope as the last run's and as this project's own.

        `only_if_changed` serves the in-sync fast path, which reconciles
        nothing and so has no scope to change, but may still carry a
        different `named` provenance than the stamp (a derived name first
        indexed unnamed, then synced with --project-name), or run on a stamp
        written before the per-project map existed. Rewriting then keeps
        `cgr check` reading the right owner; rewriting an identical stamp
        would only churn the file (PR #2251).
        """
        path = state_dir / cs.EXCLUSION_STATE_FILENAME
        state = exclusion_state(
            self._scope.exclude_paths,
            self._scope.unignore_paths,
            self._project_name,
            named=self._project_named,
        )
        if only_if_changed:
            # The loaders drop an empty `named`, so compare in that shape.
            expected = {k: v for k, v in state.items() if k != "named" or v}
            if (
                load_exclusion_state(path) == expected
                and load_project_stamps(path).get(self._project_name) == expected
            ):
                return
        save_exclusion_state(path, state)

    def record_exposes_cleanup(
        self, run: KeeperRun, state_dir: Path, cleared_by_this_run: bool
    ) -> None:
        """Owe a skipped EXPOSES cleanup to the next run, or settle it.

        A batch run's endpoint passes cover every module, so one that ran
        its cleanups settles whatever an earlier run owed; any run that
        skipped one leaves the marker for the next (issue #2193).
        """
        marker = state_dir / cs.EXPOSES_CLEANUP_PENDING_FILENAME
        try:
            if run.exposes_cleanup_skipped:
                marker.touch()
            elif cleared_by_this_run:
                marker.unlink(missing_ok=True)
        except OSError:
            # Best effort, as every other state file here: a read-only tree
            # must not lose the run to a marker it could not write.
            logger.debug(ls.EXPOSES_MARKER_NOT_UPDATED)

    def _diff_dir_against_cache(
        self,
        dir_path_str: str,
        dir_key: str,
        old_hashes: FileHashCache,
        old_dir_mtimes: DirMtimesCache,
    ) -> tuple[str | None, str | None]:
        prefix = "" if dir_key == cs.ROOT_DIR_KEY else f"{dir_key}/"
        expected_files = _direct_children(old_hashes, prefix)
        expected_dirs = _direct_children(
            (dk for dk in old_dir_mtimes if dk != cs.ROOT_DIR_KEY), prefix
        )
        try:
            actual_files, actual_dirs = _scan_dir_children(dir_path_str)
        except OSError:
            return None, dir_key

        if addition := self._first_indexable_addition(
            dir_key, prefix, actual_dirs - expected_dirs, actual_files - expected_files
        ):
            return addition, None

        for name in expected_files - actual_files:
            return None, f"{prefix}{name}"
        for name in expected_dirs - actual_dirs:
            return None, f"{prefix}{name}"

        return None, None

    def _first_indexable_addition(
        self,
        dir_key: str,
        prefix: str,
        new_dirs: set[str],
        new_files: set[str],
    ) -> str | None:
        dir_parts: tuple[str, ...] = (
            () if dir_key == cs.ROOT_DIR_KEY else tuple(dir_key.split("/"))
        )
        for name in new_dirs:
            if self._should_keep_dir(name, prefix):
                return f"{prefix}{name}"
        for name in new_files:
            if not should_skip_rel_file(
                f"{prefix}{name}",
                dir_parts,
                exclude_paths=self._scope.exclude_paths,
                unignore_paths=self._scope.unignore_paths,
            ):
                return f"{prefix}{name}"
        return None

    def _should_keep_dir(self, dirname: str, dir_prefix: str) -> bool:
        return should_keep_dir(
            dirname, dir_prefix, self._scope.exclude_paths, self._scope.unignore_paths
        )

    def stored_project_root(self) -> str | None:
        """The root the graph's project of this name was last indexed from.

        A graph that cannot answer raises instead of reading as "no root":
        the cache would then be trusted for another repository's code, and
        the Project write that follows replaces the root that shows it
        (PR #2499). A write-only sink holds no project to own.
        """
        graph = self._graph
        if graph is None:
            return None
        rows = self._graph_rows(
            cq.CYPHER_PROJECT_ROOT_PATH, {cs.KEY_PROJECT_NAME: self._project_name}
        )
        root = rows[0].get(cs.KEY_ROOT_PATH) if rows else None
        return root if isinstance(root, str) and root else None

    def drop_cache_if_graph_lost(
        self, run: KeeperRun, state_dir: Path, previous_root: str | None = None
    ) -> None:
        """Discard the hash cache when the graph no longer holds this project.

        The cache lives inside the repo, but the database is shared: cleaning
        the database while indexing another repo, an MCP wipe_database, or a
        fresh Memgraph instance voids the cache without touching it, and an
        incremental sync that trusts it would skip every file and leave the
        project silently empty.

        The same holds when another repository was synced under this name
        since: the project has modules, but they are that repository's, and
        a cache trusted here would report the other code as in sync (#2411).
        A project with no recorded root proves nothing either way.
        """
        cache_path = state_dir / cs.HASH_CACHE_FILENAME
        if not cache_path.is_file():
            return
        if (
            previous_root is not None
            and Path(previous_root).resolve() != self._repo_path.resolve()
        ):
            logger.warning(
                ls.HASH_CACHE_OTHER_ROOT.format(
                    project=self._project_name, root=previous_root
                )
            )
            self._discard_hash_cache(run, state_dir, cache_path)
            return
        fetch_all = getattr(self._ingestor, "fetch_all", None)
        if fetch_all is None:
            return
        try:
            rows = fetch_all(
                cs.CYPHER_COUNT_PROJECT_MODULES,
                {
                    cs.KEY_PROJECT_NAME: self._project_name,
                    cs.KEY_PROJECT_PREFIX: self._project_name + ".",
                },
            )
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            # A graph that cannot answer (connection refused, a sink that
            # rejects reads) cannot invalidate the cache: fail open.
            return
        try:
            # count() always yields exactly one row; anything else means the
            # sink did not really answer and cannot invalidate the cache.
            count = int(rows[0][cs.KEY_COUNT])
        except (KeyError, IndexError, TypeError, ValueError):
            return
        if count:
            return
        if self._previous_build_unfinished(state_dir):
            # Nothing was wiped: that build stopped before any of its modules
            # reached the graph.
            logger.info(ls.PREVIOUS_SYNC_UNFINISHED, project=self._project_name)
        else:
            logger.warning(ls.HASH_CACHE_ORPHANED.format(project=self._project_name))
        self._discard_hash_cache(run, state_dir, cache_path)

    def _discard_hash_cache(
        self, run: KeeperRun, state_dir: Path, cache_path: Path
    ) -> None:
        # Discarding is best-effort by intent: `missing_ok=True` already says a
        # cache that is not there is fine, and a cache that cannot be REMOVED
        # is the same situation one step later. Every other filesystem writer
        # on this path degrades quietly, so a read-only tree reaches here
        # having survived all of them and must not lose the whole indexing run
        # to a file it only wanted to delete (issue #1647).
        for stale in (
            cache_path,
            state_dir / cs.DIR_MTIMES_FILENAME,
            state_dir / cs.EXPOSES_CLEANUP_PENDING_FILENAME,
        ):
            try:
                stale.unlink(missing_ok=True)
            except OSError as e:
                # Failing to DELETE the file must not resurrect TRUSTING it.
                # The mtime fast path (`file_mtime <= cache_mtime`) skips a
                # file before its hash is ever compared, and a real run stamps
                # the cache at or after its sources, so a surviving cache makes
                # every file look unchanged against hashes already known dead:
                # the run indexes nothing, and the next run repeats it forever
                # because the graph is still empty. Ignoring the cache in
                # memory gives this run the full rebuild the discard intended.
                run.cache_discarded_in_memory = True
                logger.warning(ls.HASH_CACHE_DISCARD_FAILED, path=stale, error=e)

    @staticmethod
    def _previous_build_unfinished(state_dir: Path) -> bool:
        """Whether the hash cache is the placeholder of a build that never committed.

        A full build writes an empty cache before its first graph write
        (`TreeScan.hash_baseline`) and replaces it, stamping the parser fingerprint
        beside it, only at its commit point. An empty cache with no stamp is
        therefore a build that stopped short (Ctrl+C, a crash), and blaming a
        parser change or a wiped database for the rebuild that follows sent
        users looking for a cause that was not there (issue #2442). A graph
        built before the stamp existed has a cache naming its files, and a
        finished build that found no files stamps its parser all the same.
        """
        stamp = state_dir / cs.PARSER_FINGERPRINT_FILENAME
        return load_parser_fingerprint(stamp) is None and is_placeholder_hash_cache(
            state_dir / cs.HASH_CACHE_FILENAME
        )

    def reparse_all_if_parser_changed(self, run: KeeperRun, state_dir: Path) -> None:
        """Ignore the hash cache for this run when a parser input changed.

        The cache records which files the PREVIOUS inputs parsed; a file
        unchanged since then would be skipped, and whatever the new inputs
        emit for it (a property added to a node kind, a new edge) would
        never be written until the file happened to change. Treating the
        cache as dead re-parses every file once, and the completed run
        stamps the new fingerprint, so the next run parses nothing again
        (issue #1977). A re-parse only removes what a module DEFINES, so
        this is not a clean rebuild; the warning says what survives.
        """
        # No hash cache means a full build is coming: nothing to compare.
        if not (state_dir / cs.HASH_CACHE_FILENAME).is_file():
            return
        if self._previous_build_unfinished(state_dir):
            # The empty cache names no file to force, so this run is the full
            # build it would be anyway; what changes is the reason given.
            logger.info(ls.PREVIOUS_SYNC_UNFINISHED, project=self._project_name)
            return
        stored = load_parser_fingerprint(state_dir / cs.PARSER_FINGERPRINT_FILENAME)
        # A missing stamp on an existing graph means it was built by an
        # unknown (pre-fingerprint) parser: treat it as stale too, without
        # paying for a fingerprint computation that cannot match. The state
        # directory lets a toolchain probe that times out reuse an earlier
        # run's answer instead of reading as a missing toolchain.
        if stored is None or stored != self.parser_fingerprint(state_dir):
            run.parser_changed = True
            logger.warning(ls.PARSER_FINGERPRINT_MISMATCH)

    def parser_fingerprint(self, state_dir: Path) -> str:
        """The fingerprint of the parser inputs this run parses under."""
        return compute_parser_fingerprint(
            repo_path=self._repo_path,
            capture=self._capture,
            state_dir=state_dir,
        )

    def exclusions_match_last_run(self, run: KeeperRun, state_dir: Path) -> bool:
        # Memoised: `_process_files` asks again to decide whether the graph's
        # own module paths must join reconciliation, and the answer must be
        # the same one the sync check acted on, logged once.
        if run.exclusion_match is not None:
            return run.exclusion_match
        stored = load_exclusion_state(state_dir / cs.EXCLUSION_STATE_FILENAME)
        current = exclusion_state(self._scope.exclude_paths, self._scope.unignore_paths)
        if stored is not None:
            # The cache and directory mtimes are repository-scoped, but the
            # graph they justify is project-scoped. A stamp from another
            # project cannot authorize this project's in-sync fast path
            # (issue #1987).
            if stored.get("project") != self._project_name:
                run.cache_project_match = False
                logger.info(
                    ls.EXCLUSION_SET_CHANGED.format(previous=stored, current=current)
                )
                run.exclusion_match = False
                return False
            run.cache_project_match = True
            # `named` stays informational for readers such as `cgr check`;
            # the sync decision compares the scope itself. Leaving `named` in
            # the comparison made every run with --project-name miss the
            # in-sync fast path (issue #1981).
            stored = exclusion_scope(stored)
        if stored == current:
            run.exclusion_match = True
            return True
        # A missing stamp means an index built before it existed, whose
        # exclusion set is unknown rather than known-equal -- the posture
        # `reparse_all_if_parser_changed` takes for a missing fingerprint.
        # Reported apart from a real change so a user seeing an unexpected
        # full pass can tell which of the two they are looking at.
        if stored is None:
            run.cache_project_match = False
            # Only an index built before the stamp existed is being upgraded;
            # a repository never indexed has nothing to re-run "once", and a
            # first-time user was told otherwise (issue #2404).
            if (state_dir / cs.HASH_CACHE_FILENAME).is_file():
                logger.info(ls.EXCLUSION_STATE_MISSING)
            else:
                logger.debug(ls.EXCLUSION_STATE_FIRST_INDEX)
        else:
            logger.info(
                ls.EXCLUSION_SET_CHANGED.format(previous=stored, current=current)
            )
        run.exclusion_match = False
        return False

    def forces_reparse(self, run: KeeperRun, state_dir: Path) -> bool:
        """Whether something the caches cannot see sends this run into the
        passes: a changed overlay, an owed cleanup or prune, owed embeddings,
        or a changed exclusion set."""
        if self._generated_sources.state_changed:
            # The overlay's effect changed while the checked-in bytes did
            # not; the affected files must reparse.
            return True
        if self._single_file is not None:
            return True
        if not (state_dir / cs.HASH_CACHE_FILENAME).is_file():
            return True
        # A skipped EXPOSES cleanup is owed: nothing on disk changed, so no
        # hash below would send the run into the endpoint passes that owe it
        # (issue #2193).
        if (state_dir / cs.EXPOSES_CLEANUP_PENDING_FILENAME).exists():
            return True
        if (state_dir / cs.PRUNE_PENDING_FILENAME).exists():
            return True
        if self._embedding_sync.embeddings_owed(state_dir):
            return True
        # Nothing on disk changes when only the exclusion set does, so no
        # hash or directory mtime below can see it: a file excluded by a CLI
        # flag alone would keep the Module, Class and Method nodes the last
        # run gave it (issue #1606). `.cgrignore` was covered only
        # incidentally, by being an indexed and hashed file itself.
        return not self.exclusions_match_last_run(run, state_dir)

    def cache_matches_disk(self, run: KeeperRun, state_dir: Path) -> bool:
        """Whether every directory and file the caches record is unchanged."""
        cache_path = state_dir / cs.HASH_CACHE_FILENAME
        cache_mtime = trustworthy_cache_mtime(cache_path)
        dir_mtimes_path = state_dir / cs.DIR_MTIMES_FILENAME
        old_hashes = (
            {}
            if run.cache_discarded_in_memory or run.parser_changed
            else load_hash_cache(cache_path)
        )
        old_dir_mtimes = load_dir_mtimes(dir_mtimes_path)
        if not old_hashes or not old_dir_mtimes:
            return False
        return self._cached_dirs_unchanged(
            old_hashes, old_dir_mtimes
        ) and _cached_files_unchanged(str(self._repo_path), old_hashes, cache_mtime)

    def _cached_dirs_unchanged(
        self, old_hashes: FileHashCache, old_dir_mtimes: DirMtimesCache
    ) -> bool:
        repo_str = str(self._repo_path)
        for dir_key, cached_mtime in old_dir_mtimes.items():
            dir_path_str = (
                repo_str if dir_key == cs.ROOT_DIR_KEY else f"{repo_str}/{dir_key}"
            )
            try:
                current_mtime = os.stat(dir_path_str).st_mtime
            except OSError:
                return False
            if current_mtime == cached_mtime:
                continue
            addition, removal = self._diff_dir_against_cache(
                dir_path_str, dir_key, old_hashes, old_dir_mtimes
            )
            if addition is not None or removal is not None:
                return False
        return True
