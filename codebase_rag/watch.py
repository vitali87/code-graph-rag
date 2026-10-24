"""The real-time watcher behind `cgr watch` (issue #2433).

It was a top-level script, `realtime_updater.py`, that no wheel carried, so
an installed cgr had no watcher at all. Shipped here, it syncs exactly as a
manual sync does: the checkout and project name `cgr start` resolves, the
CLI's Memgraph settings, and `GraphUpdater.run` / `reingest` under the
checkout's sync lock, so the watcher and `cgr start --update-graph` never
write one checkout at once (issue #2441). It claims the project and puts
`cgr start`'s incomplete-run marker down around every write as that command
does (issues #2411, #2219).
"""

import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Parser
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from . import cli_help as ch
from . import constants as cs
from . import logs
from .cli_runtime import connect_memgraph
from .config import (
    CGRIGNORE_FILENAME,
    GITIGNORE_FILENAME,
    git_index_path,
    load_ignore_patterns,
)
from .constants import (
    CONTENT_EVENT_TYPES,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_MAX_WAIT_SECONDS,
    WATCHER_SLEEP_INTERVAL,
    EventType,
)
from .graph_updater import GraphUpdater, ReingestAborted
from .parser_loader import load_parsers
from .services import IngestorProtocol, QueryProtocol
from .services.graph_service import MemgraphIngestor
from .sync_guard import (
    SyncRefusedError,
    clear_own_sync_incomplete,
    mark_sync_incomplete,
    project_claim,
    sync_marker_down,
    sync_owner,
)
from .sync_lock import repo_sync_lock
from .types_defs import (
    CgrignorePatterns,
    LanguageQueries,
    PropertyValue,
    ReingestReport,
)
from .utils.path_utils import (
    is_eligible_rel_file,
    is_symlink_entry,
    is_walked_dir,
)


class PendingTimer(Protocol):
    """What the handler needs back from a `TimerFactory`.

    `daemon` is assigned before `start()`, and `cancel()` supersedes a timer
    when a newer event arrives for the same path.
    """

    daemon: bool

    def start(self) -> None: ...

    def cancel(self) -> None: ...


# `start()` is called with `self.lock` held and `_process_debounced_change`
# re-acquires that same non-reentrant lock, so a factory MUST queue its
# callback for another thread (or for a later explicit fire) rather than
# invoking it during `start()` — doing so deadlocks the handler.
TimerFactory = Callable[..., PendingTimer]
UpdaterFactory = Callable[[CgrignorePatterns], GraphUpdater]


class UnfinishedSyncError(RuntimeError):
    """A CLI sync of the project stopped part-way, so its graph may be partial."""


class WriteMarker:
    """`cgr start`'s `:IncompleteRun` marker around each watcher write (#2219).

    Down before the initial scan, a full re-index or a re-ingest's first
    write, and off once that write succeeded, so a watcher stopped part-way
    leaves a graph `cgr status` and the MCP server know to be partial. It is
    the CLI sync's own marker, so `cgr start` clears what the watcher left;
    the watcher takes off only a marker it put down itself, so one another
    writer put down since stays (review of PR 3365). Every mark holds to the
    claim `cgr watch` took on its project (#2411): once another repository
    has the name, the write is refused instead of replacing that
    repository's graph.
    """

    def __init__(
        self,
        ingestor: QueryProtocol,
        project_name: str,
        owner: dict[str, PropertyValue],
        owned_roots: list[str] | None,
    ) -> None:
        self._ingestor = ingestor
        self._project_name = project_name
        self._owner = owner
        self._owned_roots = owned_roots

    def mark(self) -> None:
        mark_sync_incomplete(
            self._ingestor, self._project_name, self._owner, self._owned_roots
        )

    def unfinished(self) -> bool:
        """Whether a CLI sync's marker is already down: one did not finish.

        Asked with the checkout lock held, so no writer of this checkout can
        still be running behind it.
        """
        return sync_marker_down(self._ingestor, self._project_name)

    def mark_scoped(self) -> None:
        """Mark before a re-ingest writes, unless a sync left the graph partial.

        A re-ingest resolves its files against the rest of the graph, and
        clearing the shared marker after it would call whole a graph an
        unfinished `cgr start` left partial. The MCP `reingest` refuses there
        for the same reason (#1705); the watcher re-indexes instead.
        """
        if self.unfinished():
            raise UnfinishedSyncError(self._project_name)
        self.mark()

    def clear(self) -> None:
        clear_own_sync_incomplete(self._ingestor, self._project_name, self._owner)


def _marked_run(
    updater: GraphUpdater,
    marker: WriteMarker | None,
    force: bool = False,
    before_run: Callable[[], None] | None = None,
) -> None:
    """Run the updater over the whole checkout between `marker`'s mark and clear.

    The caller holds the checkout's sync lock, so the marker goes down after
    a sync that held it first has taken its own off: they share one. It
    comes off once the run committed, as `cgr start` takes its own off: a
    Ctrl+C landing after the commit found the graph whole. Any other failure
    leaves it down. `before_run` runs between the mark and the run, where
    `cgr start` ensures the graph's constraints.

    A marker already down is an earlier writer's that did not finish: a
    watcher stopped part-way through a re-ingest may have deleted an
    unchanged dependent's definitions, which an incremental run skips by
    their saved hash. So the run is forced, and only that rebuild takes the
    marker off (review of PR 3365).
    """
    if marker is None:
        if before_run is not None:
            before_run()
        updater.run(force=force)
        return
    if not force and marker.unfinished():
        logger.warning(
            logs.WATCHER_FULL_RUN_OVER_UNFINISHED_SYNC.format(
                project=updater.project_name
            )
        )
        force = True
    marker.mark()
    try:
        if before_run is not None:
            before_run()
        updater.run(force=force)
    except KeyboardInterrupt:
        if updater.committed:
            marker.clear()
        raise
    marker.clear()


class IgnoreRules:
    """The ignore rules the watcher applies, and the files they derive from.

    `load_ignore_patterns` reads the root `.cgrignore` and `.gitignore` and
    asks git which files it tracks. Read once at start-up, a tracked file
    renamed within `out/` was refused as untracked and an edited ignore file
    left later events filtered by the old rules (review of PR 2490), so the
    handler re-reads them whenever one of these inputs changes. The git index
    stands for "what git tracks": `git mv`, `git add` and `git rm` rewrite it.
    """

    def __init__(self, repo_path: Path, updater_factory: UpdaterFactory) -> None:
        self.repo_path = repo_path
        self._updater_factory = updater_factory
        self.patterns = load_ignore_patterns(repo_path)
        inputs = {repo_path / CGRIGNORE_FILENAME, repo_path / GITIGNORE_FILENAME}
        if (index := git_index_path(repo_path)) is not None:
            inputs.add(index)
        self.inputs = frozenset(inputs)

    def outside_dirs(self) -> frozenset[Path]:
        """Directories of inputs the repository watch misses (a worktree's index)."""
        return frozenset(
            path.parent
            for path in self.inputs
            if not path.is_relative_to(self.repo_path)
        )

    def build_updater(self) -> GraphUpdater:
        return self._updater_factory(self.patterns)

    def reload(self) -> bool:
        """Re-read the rules; True when they changed."""
        patterns = load_ignore_patterns(self.repo_path)
        changed = patterns != self.patterns
        self.patterns = patterns
        return changed


class CodeChangeEventHandler(FileSystemEventHandler):
    """
    Handles file system events with debouncing to prevent redundant graph updates.

    The handler implements a hybrid debounce strategy:
    - Debounce: Waits for a quiet period after the last change before processing
    - Max wait: Ensures updates happen within a maximum time window, even during
                continuous editing

    This prevents the graph update process from running repeatedly when a file
    is saved multiple times in quick succession (common during active development).
    """

    def __init__(
        self,
        updater: GraphUpdater,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
        timer_factory: TimerFactory = threading.Timer,
        ignore_rules: IgnoreRules | None = None,
        write_marker: WriteMarker | None = None,
    ):
        self.updater = updater
        # The marker every write goes under (#2219); None writes unmarked, as
        # a handler built on its own around an updater does.
        self._marker = write_marker
        # Where the updater's ignore rules come from, so a change to them
        # re-syncs the graph under the new rules. None keeps the updater's
        # rules as given.
        self._ignore_rules = ignore_rules
        # Injectable so a test can drive the debounce deterministically rather
        # than racing a wall clock, which is what made these tests flaky on
        # loaded runners (issue #1005). Production always uses threading.Timer.
        self._timer_factory = timer_factory
        # Set when a scoped re-ingest fails after it may have written, so the
        # next change re-indexes the whole repository before touching it
        # (issue #1681).
        self._needs_full_rebuild = False

        self.debounce_seconds = debounce_seconds
        self.max_wait_seconds = max_wait_seconds
        self.debounce_enabled = debounce_seconds > 0

        # Thread-safe state for tracking pending changes
        self.timers: dict[str, PendingTimer] = {}
        self.first_event_time: dict[str, float] = {}
        self.pending_events: dict[str, FileSystemEvent] = {}
        self.lock = threading.Lock()
        # Debounce timers fire on separate threads, and a graph update
        # mutates shared parser state (_parsed_files, import maps, caches)
        # then deletes and recomputes every CALLS edge: two interleaved
        # updates can drop a just-registered file's edges. The whole
        # update runs as one serialized transaction (issues #1028, #1032).
        self._update_lock = threading.Lock()

        if self.debounce_enabled:
            logger.info(
                logs.WATCHER_DEBOUNCE_ACTIVE.format(
                    debounce=debounce_seconds, max_wait=max_wait_seconds
                )
            )
        else:
            logger.info(logs.WATCHER_ACTIVE)

    def _rebuild_after_failure(
        self, notice: str = logs.WATCHER_REBUILDING_AFTER_FAILURE
    ) -> bool:
        """Re-index everything after a partial re-ingest. True when the graph is whole.

        `force=True` is required, not tidiness: an incremental run skips files
        whose hashes are unchanged, and after a re-ingest that deleted subtrees
        without rebuilding them the FILES on disk are unchanged. A plain
        `run()` would therefore skip exactly the files whose nodes are missing
        and report success over a graph that is still partial.

        A rebuild that itself fails must not escape either. This runs from a
        watchdog callback, so an exception here ends the dispatcher and the
        watcher goes silently deaf -- the same failure this recovery exists to
        prevent, one level up. The flag stays set so the next change retries.
        """
        logger.warning(notice)
        try:
            self._full_run(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(logs.WATCHER_REBUILD_FAILED.format(error=exc))
            return False
        self._needs_full_rebuild = False
        return True

    def _full_run(self, force: bool = False) -> None:
        """Run the updater over the whole checkout, queued behind a running sync.

        `run` alone refuses while another writer holds the checkout's sync
        lock, and nothing retries it when that sync ends: a rules change or
        an owed rebuild would wait for some unrelated edit (review of PR
        2512). It waits instead, as the watcher's initial scan and its scoped
        reingests do; `run` then re-enters the lock this thread holds.
        """
        updater = self.updater
        with repo_sync_lock(updater.repo_path, updater.project_name, wait=True):
            _marked_run(updater, self._marker, force=force)

    def _is_relevant(self, path_str: str) -> bool:
        # The same predicate as the repository walk, not a restatement of it:
        # the watcher and the indexer have to agree about which files are in
        # the graph, and every separate copy of the rule has drifted (issues
        # #1636, #1637). The exclude and unignore sets are read from the
        # updater on every call, because `_register_generated_sources`
        # recomputes `unignore_paths` each run and a copy taken here would go
        # stale. Watchdog hands this method an ABSOLUTE path; the rule is about
        # the path inside the repository, so relativise first -- a checkout
        # under /tmp would otherwise have `tmp` as an ignored component.
        relative = self._repo_relative(Path(path_str))
        if not is_eligible_rel_file(
            relative.as_posix(),
            exclude_paths=getattr(self.updater, "exclude_paths", None),
            unignore_paths=getattr(self.updater, "unignore_paths", None),
        ):
            return False
        # The walk follows no link (issue #2451). Handed on, an outside link
        # was refused with a warning and an in-repo one re-ingested its
        # target; a deleted link is no longer a link, so its removal passes.
        if is_symlink_entry(path_str):
            logger.debug(logs.WATCHER_SYMLINK_IGNORED.format(path=relative))
            return False
        return True

    def _indexed_file_now_a_link(self, path_str: str) -> bool:
        """Whether a link now stands where the graph holds an indexed file.

        `_is_relevant` judges the path as it is now, so a file replaced by a
        link before its events were handled lost its delete and its create
        alike, and its definitions stayed until the next sync (Greptile
        review, PR #2828). Its nodes go as a deletion; the link itself is
        still never re-ingested. The hash cache names what was indexed.
        """
        return is_symlink_entry(path_str) and self.updater.has_indexed(Path(path_str))

    def _is_walked(self, directory: Path) -> bool:
        return is_walked_dir(
            self._repo_relative(directory).parts,
            exclude_paths=getattr(self.updater, "exclude_paths", None),
            unignore_paths=getattr(self.updater, "unignore_paths", None),
        )

    def _repo_relative(self, path: Path) -> Path:
        """The path as the repository sees it, mirroring `dispatch`.

        Falls back to the bare filename when the path is outside the repo (or
        the updater cannot say where the repo is, as with a test double): that
        keeps the filename rules working and simply cannot consult directory
        rules it has no directories for.
        """
        try:
            return path.relative_to(self.updater.repo_path)
        except (ValueError, AttributeError, TypeError):
            return Path(path.name)

    def dispatch(self, event: FileSystemEvent) -> None:
        # ┌─────────────────────────────────────────────────────────────────────┐
        # │                      Real-Time Graph Update Steps                   │
        # ├─────────────────────────────────────────────────────────────────────┤
        # │ Step 1: Split moves and directory events into per-file deletes     │
        # │         and creates, then drop ignored or irrelevant paths         │
        # │ Step 2: Debounce, so a burst of saves to one file becomes one job  │
        # │ Step 3: Hand the changed and deleted paths to                      │
        # │         GraphUpdater.reingest, which deletes the old subtrees,     │
        # │         re-parses the files plus their one-level dependents,       │
        # │         resolves calls in that set only and flushes (#1524)        │
        # │ Step 4: Log what was re-parsed, what depended on it, what was      │
        # │         removed, and how long it took                              │
        # └─────────────────────────────────────────────────────────────────────┘
        if self._ignore_rules is not None and not self._follow_ignore_rules(
            event, self._ignore_rules
        ):
            return
        try:
            file_events = self._file_events(event)
        except (OSError, ValueError) as exc:
            # Expanding a directory event reads the hash cache and walks the
            # destination. If either fails, the files it covers are unknown,
            # so re-index everything rather than update part of the graph.
            # Nothing may escape here: this runs in the watchdog callback.
            logger.error(logs.WATCHER_EXPANSION_FAILED.format(error=exc))
            with self._update_lock:
                self._needs_full_rebuild = True
                self._rebuild_after_failure()
            return
        for file_event in file_events:
            self._dispatch_file(file_event)

    def _follow_ignore_rules(self, event: FileSystemEvent, rules: IgnoreRules) -> bool:
        """Re-read the rules when the event touches an input; False to drop it.

        Only the repository's own files go further: the extra watch on a
        linked worktree's git directory exists for its index alone.
        """
        paths = [Path(_event_path(event.src_path))]
        if event.event_type == EventType.MOVED:
            paths.append(Path(_event_path(event.dest_path)))
        if any(path in rules.inputs for path in paths):
            self._apply_ignore_rules(rules)
        return any(path.is_relative_to(rules.repo_path) for path in paths)

    def _apply_ignore_rules(self, rules: IgnoreRules) -> None:
        """Re-sync the graph under changed rules with an updater built for them.

        A fresh updater rather than new sets on the old one: the processors
        hold their own copies of the exclude set. Its incremental run sees the
        changed exclusion stamp and reconciles the graph, indexing what the new
        rules admit (the renamed `out/` file) and deleting what they drop. A
        `git status` that rewrites the index without moving a rule costs one
        `git ls-files` and nothing more.

        A full rebuild a failed re-ingest left owed runs instead, on the new
        updater, and clears the flag only when it succeeds: the incremental
        run skips an unchanged file whose nodes that failure deleted, then
        stamps the partial graph as current (review of PR 2490).
        """
        with self._update_lock:
            if not rules.reload():
                return
            logger.info(logs.WATCHER_IGNORE_RULES_CHANGED)
            self.updater = rules.build_updater()
            if self._needs_full_rebuild:
                self._rebuild_after_failure()
                return
            try:
                self._full_run()
            except Exception as exc:  # noqa: BLE001
                # Must not escape the watchdog callback; the next change
                # re-indexes everything first, as after a failed re-ingest.
                logger.error(logs.WATCHER_IGNORE_RULES_SYNC_FAILED.format(error=exc))
                self._needs_full_rebuild = True

    def _file_events(self, event: FileSystemEvent) -> list[FileSystemEvent]:
        """Restate an event as the per-file deletions and creations it implies.

        A move is a deletion of the source plus a creation of the destination:
        editors that save atomically rename a temp file over the target, and a
        rename must retract the old path as well as index the new one. Each
        side then passes the ignore rules on its own, so a temp-file source is
        dropped while its destination is indexed. A directory deleted or moved
        away arrives as ONE event, and its files are gone from disk, so the
        updater's record of what it indexed there supplies them.
        """
        src_path = _event_path(event.src_path)
        if event.event_type == EventType.MOVED:
            # Watchdog follows a directory move with synthetic moves of
            # everything inside it; the directory's own event already
            # restated those files, so repeating them would re-ingest twice.
            if event.is_synthetic:
                return []
            dest_path = _event_path(event.dest_path)
            if not event.is_directory:
                return [FileDeletedEvent(src_path), FileCreatedEvent(dest_path)]
            return [*self._deleted_under(src_path), *self._created_under(dest_path)]
        if event.event_type == EventType.DELETED:
            # Windows reports a deleted directory as a file deletion, so the
            # indexed record, not the event, says whether it held files.
            children = self._deleted_under(src_path)
            if children or event.is_directory:
                return children
            return [event]
        if not event.is_directory:
            return [event]
        return []

    def _deleted_under(self, directory: str) -> list[FileSystemEvent]:
        return [
            FileDeletedEvent(str(path))
            for path in self.updater.indexed_files_under(Path(directory))
        ]

    def _created_under(self, directory: str) -> list[FileSystemEvent]:
        """Creations for the files now beneath `directory`.

        Directories the repository walk would not enter are pruned rather
        than walked: `_is_relevant` would drop every file under them anyway,
        and walking a moved-in `node_modules` would hold the watcher for
        nothing.
        """
        root = Path(directory)
        if not self._is_walked(root):
            return []
        files: list[Path] = []
        for current, dirs, names in os.walk(root, onerror=_raise_walk_error):
            dirs[:] = [name for name in dirs if self._is_walked(Path(current, name))]
            files.extend(Path(current) / name for name in names)
        return [FileCreatedEvent(str(path)) for path in sorted(files) if path.is_file()]

    def _dispatch_file(self, event: FileSystemEvent) -> None:
        src_path = _event_path(event.src_path)
        if not self._is_relevant(src_path):
            if not self._indexed_file_now_a_link(src_path):
                return
            event = FileDeletedEvent(src_path)

        if not self.debounce_enabled:
            # No debouncing: process immediately (legacy behaviour)
            self._process_change(event)
            return

        path = Path(src_path)
        relative_path_str = str(path.relative_to(self.updater.repo_path))
        current_time = time.time()

        with self.lock:
            pending = self.pending_events.get(relative_path_str)
            change = _coalesce(pending, event)
            if change is None or change is pending:
                # Nothing new to apply (a read, or the `closed` that ends a
                # write already pending), so the window is neither opened nor
                # extended and a read never logs as a change.
                return
            self.pending_events[relative_path_str] = change

            # Track the first event time for the max-wait calculation
            if relative_path_str not in self.first_event_time:
                self.first_event_time[relative_path_str] = current_time
                logger.info(
                    logs.CHANGE_DEBOUNCING.format(
                        event_type=change.event_type,
                        name=path.name,
                        debounce=self.debounce_seconds,
                    )
                )

            if relative_path_str in self.timers:
                self.timers[relative_path_str].cancel()
                logger.debug(logs.DEBOUNCE_RESET.format(path=relative_path_str))

            time_since_first = current_time - self.first_event_time[relative_path_str]

            if time_since_first >= self.max_wait_seconds:
                # Max wait exceeded: process immediately
                logger.info(
                    logs.DEBOUNCE_MAX_WAIT.format(
                        max_wait=self.max_wait_seconds, path=relative_path_str
                    )
                )
                self._schedule_immediate_processing(relative_path_str)
            else:
                remaining_wait = self.max_wait_seconds - time_since_first
                effective_delay = min(self.debounce_seconds, remaining_wait)
                timer = self._timer_factory(
                    effective_delay,
                    self._process_debounced_change,
                    args=[relative_path_str],
                )
                timer.daemon = True
                self.timers[relative_path_str] = timer
                timer.start()

                logger.debug(
                    logs.DEBOUNCE_SCHEDULED.format(
                        path=relative_path_str,
                        debounce=self.debounce_seconds,
                        remaining=f"{remaining_wait:.1f}",
                    )
                )

    def _schedule_immediate_processing(self, relative_path_str: str) -> None:
        """Process a file change immediately (called when max wait is exceeded)."""
        # Use a zero-delay timer to process in the timer thread
        timer = self._timer_factory(
            0, self._process_debounced_change, args=[relative_path_str]
        )
        timer.daemon = True
        self.timers[relative_path_str] = timer
        timer.start()

    def _process_debounced_change(self, relative_path_str: str) -> None:
        """Process a debounced file change after the timer fires."""
        with self.lock:
            # Retrieve and clear pending state for this file
            event = self.pending_events.pop(relative_path_str, None)
            self.first_event_time.pop(relative_path_str, None)
            self.timers.pop(relative_path_str, None)

        if event is None:
            logger.warning(logs.DEBOUNCE_NO_EVENT.format(path=relative_path_str))
            return

        logger.info(logs.DEBOUNCE_PROCESSING.format(path=relative_path_str))
        self._process_change(event)

    def _process_change(self, event: FileSystemEvent) -> None:
        """Execute the actual graph update for a file change."""
        with self._update_lock:
            self._process_change_locked(event)

    def _process_change_locked(self, event: FileSystemEvent) -> None:
        """Re-ingest one changed file, holding the updater lock.

        The whole recipe (delete what the file contributed, re-parse it and
        its dependents, resolve calls in that set only, restore the rest of
        the inbound edges) lives in GraphUpdater.reingest, shared with the
        MCP ``reingest`` tool (issue #1524).
        """
        src_path = _event_path(event.src_path)

        # Without a debounce every event arrives here, and the `closed` after
        # a write would re-ingest the file a second time.
        if event.event_type not in CONTENT_EVENT_TYPES:
            return

        path = Path(src_path)
        logger.warning(
            logs.CHANGE_DETECTED.format(event_type=event.event_type, path=path)
        )
        # A previous scoped re-ingest died part way through, so the graph may be
        # missing the subtrees it deleted and never rebuilt. Resolving this
        # change against that state would compound the damage, so restore a
        # whole graph first (issue #1681).
        if self._needs_full_rebuild and not self._rebuild_after_failure():
            # The rebuild failed too, so the graph is still partial. Skip the
            # scoped work rather than resolve this change against it; the flag
            # stays set and the next change tries again.
            return
        try:
            self._reingest(path, deleted=event.event_type == EventType.DELETED)
        except (ValueError, ReingestAborted) as exc:
            # A refusal (a symlink resolving outside the repo, a directory where
            # a file was expected) is raised while the paths are split, and an
            # abort while the call was still READING the graph. Neither wrote
            # anything, so the updater is still valid and later events run.
            if isinstance(exc.__cause__, UnfinishedSyncError):
                self._rebuild_unfinished_sync()
                return
            logger.warning(logs.WATCHER_REINGEST_REFUSED.format(path=path, error=exc))
            return
        except Exception as exc:  # noqa: BLE001
            # Anything else may have deleted the affected subtrees and never
            # rebuilt them. Letting it escape would end this callback with the
            # updater retained, so every later event resolves against a graph
            # that no longer matches its registry. Mirrors the MCP tool's
            # posture (`_reingest_sync`), adapted for a long-lived watcher:
            # recover on the next event rather than refusing for ever.
            logger.error(logs.WATCHER_REINGEST_FAILED.format(path=path, error=exc))
            self._needs_full_rebuild = True
            return
        logger.success(logs.GRAPH_UPDATED.format(name=path.name))

    def _reingest(self, path: Path, *, deleted: bool) -> None:
        """Re-ingest one path, under the marker when the watcher carries one.

        Every mutating path marks, as the MCP `reingest` does (#1705). The
        marker goes down from the re-ingest's `before_write`, where its
        read-only prologue ends, so a refused or empty re-ingest leaves none,
        and it comes off once the re-ingest returned. A failure after the
        first write leaves it down until the full re-index owed then clears
        it (issue #1681).
        """
        marker = self._marker
        if marker is None:
            _reingest_path(self.updater.reingest, path, deleted)
            return
        updater = self.updater
        # Held from before the first write until the marker is off, and
        # re-entered by the re-ingest itself: let go in between, a sync of
        # the checkout could mark for this clear to take off (review of PR
        # 3365).
        with repo_sync_lock(updater.repo_path, updater.project_name, wait=True):
            _reingest_path(
                partial(updater.reingest, before_write=marker.mark_scoped),
                path,
                deleted,
            )
            if updater.reingest_mutated:
                marker.clear()

    def _rebuild_unfinished_sync(self) -> None:
        """Re-index the whole checkout over a graph an unfinished sync left.

        The re-ingest stopped before writing; the full re-index covers its
        change and, once whole, takes the unfinished sync's marker off.
        """
        self._needs_full_rebuild = True
        self._rebuild_after_failure(
            logs.WATCHER_SYNC_UNFINISHED.format(project=self.updater.project_name)
        )


def _reingest_path(
    reingest: Callable[..., ReingestReport], path: Path, deleted: bool
) -> None:
    if deleted:
        reingest((), deleted=(path,))
    else:
        reingest((path,))


def _coalesce(
    pending: FileSystemEvent | None, event: FileSystemEvent
) -> FileSystemEvent | None:
    """The change a path's debounce window applies once `event` joins it.

    Linux reports one write as `created, opened, modified, closed` and a read
    as `opened, closed_no_write`, so the LAST event is rarely the one that
    matters; keeping it handed the timer a `closed` that
    `_process_change_locked` skips, and no edit reached the graph (issue
    #2430). The newest created, modified or deleted wins: both re-ingest
    calls read the path from disk when the timer fires, so a delete then a
    re-create applies the new content and a create then a delete applies the
    delete. A `closed` stands in for a write only when the window holds
    nothing else, as after a write through mmap, which raises no `modified`.
    """
    if event.event_type in CONTENT_EVENT_TYPES:
        return event
    if event.event_type == EventType.CLOSED and pending is None:
        return FileModifiedEvent(_event_path(event.src_path))
    return pending


def _raise_walk_error(error: OSError) -> None:
    # os.walk skips a directory it cannot list unless told otherwise, which
    # would index a moved directory only in part.
    raise error


def _event_path(raw: bytes | str) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


def start_watcher(
    repo_path: Path,
    *,
    project_name: str,
    project_named: bool,
    batch_size: int,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    assume_yes: bool = False,
) -> None:
    """Scan the checkout, then keep its graph in sync until Ctrl+C.

    `project_name` and `project_named` are what `cgr start` resolves for the
    same checkout and options, so both commands update one project and keep
    one set of sync state (issue #2432). The connection is the CLI's: the
    MEMGRAPH_* settings, not flags of the watcher's own.

    In `cgr start`'s order: the project is claimed for this checkout and
    the claim is held to on every write (#2411), the scan's incomplete-run
    marker goes down (#2219), the graph's constraints are ensured, and the
    scan runs; every later write goes under the marker too. Raises
    `SyncRefusedError`, before the scan writes, when the project is another
    repository's (unless `assume_yes`, as `cgr start --yes`), its owner
    cannot be read, the marker cannot go down, or the constraints cannot be
    ensured; the marker stays down in that last case.
    """
    parsers, queries = load_parsers()
    refused: SyncRefusedError | None = None
    with connect_memgraph(batch_size) as ingestor:
        try:
            with (
                project_claim(
                    ingestor, project_name, repo_path, assume_yes=assume_yes
                ) as owned_roots,
                sync_owner() as owner,
            ):
                _run_watcher_loop(
                    ingestor,
                    repo_path,
                    parsers,
                    queries,
                    debounce_seconds,
                    max_wait_seconds,
                    project_name,
                    project_named,
                    WriteMarker(ingestor, project_name, owner, owned_roots),
                    partial(_ensure_constraints, ingestor, project_name),
                )
        except SyncRefusedError as refusal:
            # Raised past the connection, which logs what leaves it as a
            # failure with a traceback; the CLI reports this one itself.
            refused = refusal
    if refused is not None:
        raise refused


def _ensure_constraints(ingestor: MemgraphIngestor, project_name: str) -> None:
    """Create the graph's constraints and indexes before the first scan writes.

    As `cgr start` does before each sync (review of PR 3365): without them a
    fresh or unmigrated graph lets parallel MERGE flushes create duplicate
    nodes, and lookups run unindexed. Once per watcher: they outlive every
    later write, and a `cgr start --clean` that wipes the graph ensures them
    again in its own sync. Their migration can purge, so this runs with the
    scan's marker down, and a failure leaves it there.
    """
    try:
        ingestor.ensure_constraints()
    except Exception as exc:
        raise SyncRefusedError(
            cs.CLI_ERR_GRAPH_CONSTRAINTS_FAILED.format(project=project_name, error=exc)
        ) from exc


def effective_max_wait(debounce_seconds: float, max_wait_seconds: float) -> float:
    """The max wait, raised to the debounce when it is shorter.

    A max wait below the debounce would cut every quiet period short, so the
    debounce would never be what decides when a burst is applied.
    """
    if 0 < max_wait_seconds < debounce_seconds:
        logger.warning(
            logs.DEBOUNCE_MAX_WAIT_ADJUSTED.format(
                max_wait=max_wait_seconds, debounce=debounce_seconds
            )
        )
        return debounce_seconds
    return max_wait_seconds


def script_args(argv: Sequence[str]) -> list[str]:
    """The `cgr` arguments `python realtime_updater.py ARGV` stands for.

    The contributor script took the repository as its first argument; `cgr
    watch` takes it as `--repo-path`, as `cgr start` does. Everything else
    passes through unchanged.
    """
    args = list(argv)
    if args and not args[0].startswith(cs.CHAR_HYPHEN):
        args[:1] = [cs.CLI_OPT_REPO_PATH, args[0]]
    return [ch.CLICommandName.WATCH.value, *args]


def _initial_scan(
    updater: GraphUpdater,
    marker: WriteMarker | None = None,
    prepare_graph: Callable[[], None] | None = None,
) -> None:
    """Build the context for real-time updates with a full scan.

    A watcher started while a sync of the checkout is running waits for it
    and then scans the graph it finished, as its later reingests do, rather
    than refusing to start (issue #2441). `prepare_graph` runs under the
    scan's marker before it writes, as `cgr start` ensures its constraints.
    """
    logger.info(logs.INITIAL_SCAN)
    with repo_sync_lock(updater.repo_path, updater.project_name, wait=True):
        _marked_run(updater, marker, before_run=prepare_graph)
    logger.success(logs.INITIAL_SCAN_DONE)


def _run_watcher_loop(
    ingestor: IngestorProtocol,
    repo_path_obj: Path,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    debounce_seconds: float,
    max_wait_seconds: float,
    project_name: str,
    project_named: bool,
    marker: WriteMarker | None = None,
    prepare_graph: Callable[[], None] | None = None,
) -> None:
    ignore_rules = IgnoreRules(
        repo_path_obj,
        partial(
            _watcher_updater,
            ingestor,
            repo_path_obj,
            parsers,
            queries,
            project_name,
            project_named,
        ),
    )
    updater = ignore_rules.build_updater()
    _initial_scan(updater, marker, prepare_graph)

    event_handler = CodeChangeEventHandler(
        updater,
        debounce_seconds=debounce_seconds,
        max_wait_seconds=max_wait_seconds,
        ignore_rules=ignore_rules,
        write_marker=marker,
    )
    observer = Observer()
    observer.schedule(event_handler, str(repo_path_obj), recursive=True)
    for directory in ignore_rules.outside_dirs():
        observer.schedule(event_handler, str(directory), recursive=False)
    observer.start()
    logger.info(logs.WATCHING.format(path=repo_path_obj, project=updater.project_name))

    try:
        while True:
            time.sleep(WATCHER_SLEEP_INTERVAL)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def _watcher_updater(
    ingestor: IngestorProtocol,
    repo_path_obj: Path,
    parsers: Mapping[cs.SupportedLanguage, Parser],
    queries: Mapping[cs.SupportedLanguage, LanguageQueries],
    project_name: str,
    project_named: bool,
    patterns: CgrignorePatterns,
) -> GraphUpdater:
    # The name `cgr start --repo-path` gives this checkout, not GraphUpdater's
    # bare-directory fallback: live updates must land in the project every
    # other command reads, and the two writers share one hash cache and
    # exclusion stamp (issue #2432).
    #
    # The same ignore sets `cgr index` loads, or the initial scan skips what
    # `.cgrignore`, `.gitignore` and the git-tracked rescue decide and the
    # event filter, which reads them from the updater, drops their edits
    # (review of PR 2490).
    return GraphUpdater(
        ingestor,
        repo_path_obj,
        parsers,
        queries,
        unignore_paths=patterns.unignore or None,
        exclude_paths=patterns.exclude or None,
        project_name=project_name,
        project_named=project_named,
    )
