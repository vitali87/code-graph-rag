import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Protocol

import typer
from loguru import logger
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from codebase_rag import cli_help as ch
from codebase_rag import logs
from codebase_rag import tool_errors as te
from codebase_rag.config import settings
from codebase_rag.constants import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_MAX_WAIT_SECONDS,
    IGNORE_PATTERNS,
    LOG_LEVEL_INFO,
    REALTIME_LOGGER_FORMAT,
    WATCHER_SLEEP_INTERVAL,
    EventType,
)
from codebase_rag.graph_updater import GraphUpdater, ReingestAborted
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.utils.path_utils import (
    is_ignored_filename,
    is_unconditionally_ignored_filename,
    unignore_names_this_file,
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
    ):
        self.updater = updater
        # Injectable so a test can drive the debounce deterministically rather
        # than racing a wall clock, which is what made these tests flaky on
        # loaded runners (issue #1005). Production always uses threading.Timer.
        self._timer_factory = timer_factory
        self.ignore_patterns = IGNORE_PATTERNS
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
        path = Path(path_str)
        # Shared with the repository walk. These two predicates answer the same
        # question and drifted apart once already (issue #1636).
        if is_unconditionally_ignored_filename(path.name):
            return False
        # The rescuable half (.min.js, .min.css) is ignored unless the run's
        # unignore set names the file (issue #1637). Read from the updater
        # rather than stored at construction: `_register_generated_sources`
        # recomputes `unignore_paths` every run, so a copy taken here would
        # go stale. Without this the watcher drops edits to a file the walk
        # indexed, and the two consumers disagree again (issue #1636).
        # Watchdog hands this method an ABSOLUTE path, while both checks below
        # are about the path INSIDE the repository. Relativise once, here:
        # matching the absolute form against repo-relative unignore patterns
        # left only the bare-filename branch working, and testing the absolute
        # form for ignored components makes the repository's own location
        # decide the answer -- a checkout under /tmp has `tmp` as a component,
        # so every file in it was dropped, first-party sources included.
        relative = self._repo_relative(path)
        if is_ignored_filename(path.name):
            unignore_paths = getattr(self.updater, "unignore_paths", None)
            # The rescuable half (.min.js, .min.css) is ignored unless the run's
            # unignore set names the file (issue #1637). Read from the updater
            # rather than stored at construction: `_register_generated_sources`
            # recomputes `unignore_paths` every run, so a copy taken here would
            # go stale.
            if not (
                unignore_paths
                and unignore_names_this_file(relative.as_posix(), unignore_paths)
            ):
                return False
        return all(part not in self.ignore_patterns for part in relative.parts)

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
        # │ Step 1: Drop events for directories and ignored or irrelevant      │
        # │         paths before they cost anything                            │
        # │ Step 2: Debounce, so a burst of saves to one file becomes one job  │
        # │ Step 3: Hand the changed and deleted paths to                      │
        # │         GraphUpdater.reingest, which deletes the old subtrees,     │
        # │         re-parses the files plus their one-level dependents,       │
        # │         resolves calls in that set only and flushes (#1524)        │
        # │ Step 4: Log what was re-parsed, what depended on it, what was      │
        # │         removed, and how long it took                              │
        # └─────────────────────────────────────────────────────────────────────┘
        src_path = event.src_path
        if isinstance(src_path, bytes):
            src_path = src_path.decode()

        if event.is_directory or not self._is_relevant(src_path):
            return

        if not self.debounce_enabled:
            # No debouncing: process immediately (legacy behaviour)
            self._process_change(event)
            return

        path = Path(src_path)
        relative_path_str = str(path.relative_to(self.updater.repo_path))
        current_time = time.time()

        with self.lock:
            # Track the first event time for the max-wait calculation
            if relative_path_str not in self.first_event_time:
                self.first_event_time[relative_path_str] = current_time
                logger.info(
                    logs.CHANGE_DEBOUNCING.format(
                        event_type=event.event_type,
                        name=path.name,
                        debounce=self.debounce_seconds,
                    )
                )

            self.pending_events[relative_path_str] = event

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
        src_path = event.src_path
        if isinstance(src_path, bytes):
            src_path = src_path.decode()

        # Only process events that change file content; skip read-only events
        # like "opened" or "closed_no_write" that don't modify the file
        relevant_events = {
            EventType.MODIFIED,
            EventType.CREATED,
            EventType.DELETED,  # watchdog deletion event
        }
        if event.event_type not in relevant_events:
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


def start_watcher(
    repo_path: str,
    host: str,
    port: int,
    batch_size: int | None = None,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
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
        )


def _run_watcher_loop(
    ingestor,
    repo_path_obj,
    parsers,
    queries,
    debounce_seconds: float,
    max_wait_seconds: float,
):
    updater = GraphUpdater(ingestor, repo_path_obj, parsers, queries)

    # Initial full scan builds the context for real-time updates
    logger.info(logs.INITIAL_SCAN)
    updater.run()
    logger.success(logs.INITIAL_SCAN_DONE)

    event_handler = CodeChangeEventHandler(
        updater,
        debounce_seconds=debounce_seconds,
        max_wait_seconds=max_wait_seconds,
    )
    observer = Observer()
    observer.schedule(event_handler, str(repo_path_obj), recursive=True)
    observer.start()
    logger.info(logs.WATCHING.format(path=repo_path_obj))

    try:
        while True:
            time.sleep(WATCHER_SLEEP_INTERVAL)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


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
    logger.add(sys.stdout, format=REALTIME_LOGGER_FORMAT, level=LOG_LEVEL_INFO)
    logger.info(logs.LOGGER_CONFIGURED)

    # Validate max_wait is greater than debounce when both are enabled
    if debounce > 0 and max_wait > 0 and max_wait < debounce:
        logger.warning(
            logs.DEBOUNCE_MAX_WAIT_ADJUSTED.format(max_wait=max_wait, debounce=debounce)
        )
        max_wait = debounce

    start_watcher(repo_path, host, port, batch_size, debounce, max_wait)


if __name__ == "__main__":
    typer.run(main)
