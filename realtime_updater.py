import os
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated, Protocol

import typer
from loguru import logger
from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from codebase_rag import cli_help as ch
from codebase_rag import logs
from codebase_rag import tool_errors as te
from codebase_rag.config import (
    CGRIGNORE_FILENAME,
    GITIGNORE_FILENAME,
    git_index_path,
    load_ignore_patterns,
    settings,
)
from codebase_rag.constants import (
    CONTENT_EVENT_TYPES,
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_MAX_WAIT_SECONDS,
    LOG_LEVEL_INFO,
    REALTIME_LOGGER_FORMAT,
    WATCHER_SLEEP_INTERVAL,
    EventType,
)
from codebase_rag.graph_updater import GraphUpdater, ReingestAborted
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.types_defs import CgrignorePatterns
from codebase_rag.utils.path_utils import (
    derive_project_name,
    is_eligible_rel_file,
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
    ):
        self.updater = updater
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

    def _rebuild_after_failure(self) -> bool:
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
        logger.warning(logs.WATCHER_REBUILDING_AFTER_FAILURE)
        try:
            self.updater.run(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.error(logs.WATCHER_REBUILD_FAILED.format(error=exc))
            return False
        self._needs_full_rebuild = False
        return True

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
        return is_eligible_rel_file(
            relative.as_posix(),
            exclude_paths=getattr(self.updater, "exclude_paths", None),
            unignore_paths=getattr(self.updater, "unignore_paths", None),
        )

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
                self.updater.run()
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
            return

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
            if event.event_type == EventType.DELETED:
                self.updater.reingest((), deleted=(path,))
            else:
                self.updater.reingest((path,))
        except (ValueError, ReingestAborted) as exc:
            # A refusal (a symlink resolving outside the repo, a directory where
            # a file was expected) is raised while the paths are split, and an
            # abort while the call was still READING the graph. Neither wrote
            # anything, so the updater is still valid and later events run.
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
    repo_path: str,
    host: str,
    port: int,
    batch_size: int | None = None,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    project_name: str | None = None,
) -> None:
    repo_path_obj = Path(repo_path).resolve()
    parsers, queries = load_parsers()

    effective_batch_size = settings.resolve_batch_size(batch_size)

    with MemgraphIngestor(
        host=host,
        port=port,
        batch_size=effective_batch_size,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
    ) as ingestor:
        _run_watcher_loop(
            ingestor,
            repo_path_obj,
            parsers,
            queries,
            debounce_seconds,
            max_wait_seconds,
            project_name,
        )


def _run_watcher_loop(
    ingestor,
    repo_path_obj,
    parsers,
    queries,
    debounce_seconds: float,
    max_wait_seconds: float,
    project_name: str | None = None,
):
    ignore_rules = IgnoreRules(
        repo_path_obj,
        partial(
            _watcher_updater, ingestor, repo_path_obj, parsers, queries, project_name
        ),
    )
    updater = ignore_rules.build_updater()

    # Initial full scan builds the context for real-time updates
    logger.info(logs.INITIAL_SCAN)
    updater.run()
    logger.success(logs.INITIAL_SCAN_DONE)

    event_handler = CodeChangeEventHandler(
        updater,
        debounce_seconds=debounce_seconds,
        max_wait_seconds=max_wait_seconds,
        ignore_rules=ignore_rules,
    )
    observer = Observer()
    observer.schedule(event_handler, str(repo_path_obj), recursive=True)
    for directory in ignore_rules.outside_dirs():
        observer.schedule(event_handler, str(directory), recursive=False)
    observer.start()
    logger.info(logs.WATCHING.format(path=repo_path_obj))

    try:
        while True:
            time.sleep(WATCHER_SLEEP_INTERVAL)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


def _watcher_updater(
    ingestor,
    repo_path_obj: Path,
    parsers,
    queries,
    project_name: str | None,
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
        project_name=project_name or derive_project_name(repo_path_obj),
        project_named=project_name is not None,
    )


def _validate_positive_int(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 1:
        raise typer.BadParameter(te.INVALID_POSITIVE_INT.format(value=value))
    return value


def _validate_non_negative_float(value: float) -> float:
    if value < 0:
        raise typer.BadParameter(te.INVALID_NON_NEGATIVE_FLOAT.format(value=value))
    return value


def main(
    repo_path: Annotated[str, typer.Argument(help=ch.HELP_REPO_PATH_WATCH)],
    host: Annotated[
        str, typer.Option(help=ch.HELP_MEMGRAPH_HOST)
    ] = settings.MEMGRAPH_HOST,
    port: Annotated[
        int, typer.Option(help=ch.HELP_MEMGRAPH_PORT)
    ] = settings.MEMGRAPH_PORT,
    batch_size: Annotated[
        int | None,
        typer.Option(
            help=ch.HELP_BATCH_SIZE,
            callback=_validate_positive_int,
        ),
    ] = None,
    debounce: Annotated[
        float,
        typer.Option(
            "--debounce",
            "-d",
            help=ch.HELP_DEBOUNCE,
            callback=_validate_non_negative_float,
        ),
    ] = DEFAULT_DEBOUNCE_SECONDS,
    max_wait: Annotated[
        float,
        typer.Option(
            "--max-wait",
            "-m",
            help=ch.HELP_MAX_WAIT,
            callback=_validate_non_negative_float,
        ),
    ] = DEFAULT_MAX_WAIT_SECONDS,
    project_name: Annotated[
        str | None,
        typer.Option("--project-name", help=ch.HELP_PROJECT_NAME_WATCH),
    ] = None,
) -> None:
    """
    Watch a repository for file changes and update the knowledge graph in real-time.

    The watcher uses a hybrid debouncing strategy to efficiently handle rapid file saves:

    - DEBOUNCE: After a file change, waits for a quiet period before processing.
      This batches rapid saves into a single update.

    - MAX_WAIT: Ensures updates happen within a maximum time window, even during
      continuous editing. Prevents indefinite delays.

    Examples:

        # Default settings (5s debounce, 30s max wait)
        python realtime_updater.py /path/to/repo

        # More aggressive batching for background monitoring
        python realtime_updater.py /path/to/repo --debounce 10 --max-wait 60

        # Quick feedback for demos
        python realtime_updater.py /path/to/repo --debounce 2 --max-wait 10

        # Disable debouncing (legacy behavior)
        python realtime_updater.py /path/to/repo --debounce 0
    """
    logger.remove()
    logger.add(
        sys.stdout,
        format=REALTIME_LOGGER_FORMAT,
        level=LOG_LEVEL_INFO,
        backtrace=False,
        diagnose=False,
    )
    logger.info(logs.LOGGER_CONFIGURED)

    # Validate max_wait is greater than debounce when both are enabled
    if debounce > 0 and max_wait > 0 and max_wait < debounce:
        logger.warning(
            logs.DEBOUNCE_MAX_WAIT_ADJUSTED.format(max_wait=max_wait, debounce=debounce)
        )
        max_wait = debounce

    start_watcher(repo_path, host, port, batch_size, debounce, max_wait, project_name)


if __name__ == "__main__":
    typer.run(main)
