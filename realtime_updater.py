import sys
import time
from pathlib import Path
from typing import Annotated

import typer
from loguru import logger
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from codebase_rag import cli_help as ch
from codebase_rag import logs
from codebase_rag import tool_errors as te
from codebase_rag.config import settings
from codebase_rag.constants import (
    CYPHER_DELETE_CALLS,
    CYPHER_DELETE_MODULE,
    IGNORE_PATTERNS,
    IGNORE_SUFFIXES,
    KEY_PATH,
    LOG_LEVEL_INFO,
    REALTIME_LOGGER_FORMAT,
    WATCHER_SLEEP_INTERVAL,
    EventType,
    SupportedLanguage,
)
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_spec import get_language_spec
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryProtocol
from codebase_rag.services.graph_service import MemgraphIngestor


class CodeChangeEventHandler(FileSystemEventHandler):
    def __init__(self, updater: GraphUpdater):
        self.updater = updater
        self.ignore_patterns = IGNORE_PATTERNS
        self.ignore_suffixes = IGNORE_SUFFIXES
        logger.info(logs.WATCHER_ACTIVE)

    def _is_relevant(self, path_str: str) -> bool:
        path = Path(path_str)
        if any(path.name.endswith(suffix) for suffix in self.ignore_suffixes):
            return False
        return all(part not in self.ignore_patterns for part in path.parts)

    def dispatch(self, event: FileSystemEvent) -> None:
        # (H) ┌─────────────────────────────────────────────────────────────────────┐
        # (H) │                      Real-Time Graph Update Steps                   │
        # (H) ├─────────────────────────────────────────────────────────────────────┤
        # (H) │ Step 1: Delete all old data from the graph for this file           │
        # (H) │         Provides a clean slate for the updated information         │
        # (H) │ Step 2: Clear the specific in-memory state for the file            │
        # (H) │         Prevents stale in-memory representations                   │
        # (H) │ Step 3: Re-parse the file if it was modified or created            │
        # (H) │         Rebuilds in-memory state (AST, function registry)          │
        # (H) │ Step 4: Re-process all function calls across the entire codebase   │
        # (H) │         Fixes "island" problem - changes reflect in all relations  │
        # (H) │ Step 5: Flush all collected changes to the database                │
        # (H) └─────────────────────────────────────────────────────────────────────┘
        src_path = event.src_path
        if isinstance(src_path, bytes):
            src_path = src_path.decode()

        if event.is_directory or not self._is_relevant(src_path):
            return

        ingestor = self.updater.ingestor
        if not isinstance(ingestor, QueryProtocol):
            logger.warning(logs.WATCHER_SKIP_NO_QUERY)
            return

        path = Path(src_path)
        relative_path_str = path.relative_to(self.updater.repo_path).as_posix()

        logger.warning(
            logs.CHANGE_DETECTED.format(event_type=event.event_type, path=path)
        )

        # (H) Step 1
        ingestor.execute_write(CYPHER_DELETE_MODULE, {KEY_PATH: relative_path_str})
        logger.debug(logs.DELETION_QUERY.format(path=relative_path_str))

        # (H) Step 2
        self.updater.remove_file_from_state(path)

        # (H) Step 3
        if event.event_type in (EventType.MODIFIED, EventType.CREATED):
            lang_config = get_language_spec(path.suffix)
            if (
                lang_config
                and isinstance(lang_config.language, SupportedLanguage)
                and lang_config.language in self.updater.parsers
            ):
                if result := self.updater.factory.definition_processor.process_file(
                    path,
                    lang_config.language,
                    self.updater.queries,
                    self.updater.factory.structure_processor.structural_elements,
                ):
                    root_node, language = result
                    self.updater.ast_cache[path] = (root_node, language)

        # (H) Step 4
        logger.info(logs.RECALC_CALLS)
        ingestor.execute_write(CYPHER_DELETE_CALLS)
        self.updater._process_function_calls()

        # (H) Step 5
        self.updater.ingestor.flush_all()
        logger.success(logs.GRAPH_UPDATED.format(name=path.name))


def start_watcher(
    repo_path: str, host: str, port: int, batch_size: int | None = None
) -> None:
    repo_path_obj = Path(repo_path).resolve()
    parsers, queries = load_parsers()

    effective_batch_size = settings.resolve_batch_size(batch_size)

    with MemgraphIngestor(
        host=host,
        port=port,
        batch_size=effective_batch_size,
    ) as ingestor:
        _run_watcher_loop(ingestor, repo_path_obj, parsers, queries)


def _run_watcher_loop(ingestor, repo_path_obj, parsers, queries):
    updater = GraphUpdater(ingestor, repo_path_obj, parsers, queries)

    # (H) Initial full scan builds the complete context for real-time updates
    logger.info(logs.INITIAL_SCAN)
    updater.run()
    logger.success(logs.INITIAL_SCAN_DONE)

    event_handler = CodeChangeEventHandler(updater)
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
) -> None:
    logger.remove()
    logger.add(sys.stdout, format=REALTIME_LOGGER_FORMAT, level=LOG_LEVEL_INFO)
    logger.info(logs.LOGGER_CONFIGURED)
    start_watcher(repo_path, host, port, batch_size)


if __name__ == "__main__":
    typer.run(main)
