import argparse
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from codebase_rag.config import IGNORE_PATTERNS, IGNORE_SUFFIXES, settings
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_config import get_language_config
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services import QueryProtocol
from codebase_rag.services.graph_service import MemgraphIngestor


class EventType(StrEnum):
    MODIFIED = "modified"
    CREATED = "created"


class CodeChangeEventHandler(FileSystemEventHandler):
    def __init__(self, updater: GraphUpdater):
        self.updater = updater
        self.ignore_patterns = IGNORE_PATTERNS
        self.ignore_suffixes = IGNORE_SUFFIXES
        logger.info("File watcher is now active.")

    def _is_relevant(self, path_str: str) -> bool:
        path = Path(path_str)
        if any(path.name.endswith(suffix) for suffix in self.ignore_suffixes):
            return False
        return all(part not in self.ignore_patterns for part in path.parts)

    def dispatch(self, event: Any) -> None:
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
        if event.is_directory or not self._is_relevant(event.src_path):
            return

        ingestor = self.updater.ingestor
        if not isinstance(ingestor, QueryProtocol):
            logger.warning(
                "Ingestor does not support querying, skipping real-time update."
            )
            return

        path = Path(event.src_path)
        relative_path_str = str(path.relative_to(self.updater.repo_path))

        logger.warning(
            f"Change detected: {event.event_type} on {path}. Updating graph."
        )

        # (H) Step 1
        delete_query = "MATCH (m:Module {path: $path})-[*0..]->(c) DETACH DELETE m, c"
        ingestor.execute_write(delete_query, {"path": relative_path_str})
        logger.debug(f"Ran deletion query for path: {relative_path_str}")

        # (H) Step 2
        self.updater.remove_file_from_state(path)

        # (H) Step 3
        if event.event_type in (EventType.MODIFIED, EventType.CREATED):
            lang_config = get_language_config(path.suffix)
            if lang_config and lang_config.name in self.updater.parsers:
                if result := self.updater.factory.definition_processor.process_file(
                    path,
                    lang_config.name,
                    self.updater.queries,
                    self.updater.factory.structure_processor.structural_elements,
                ):
                    root_node, language = result
                    self.updater.ast_cache[path] = (root_node, language)

        # (H) Step 4
        logger.info("Recalculating all function call relationships for consistency...")
        ingestor.execute_write("MATCH ()-[r:CALLS]->() DELETE r")
        self.updater._process_function_calls()

        # (H) Step 5
        self.updater.ingestor.flush_all()
        logger.success(f"Graph updated successfully for change in: {path.name}")


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
        updater = GraphUpdater(ingestor, repo_path_obj, parsers, queries)

        # (H) Initial full scan builds the complete context for real-time updates
        logger.info("Performing initial full codebase scan...")
        updater.run()
        logger.success("Initial scan complete. Starting real-time watcher.")

        event_handler = CodeChangeEventHandler(updater)
        observer = Observer()
        observer.schedule(event_handler, str(repo_path_obj), recursive=True)
        observer.start()
        logger.info(f"Watching for changes in: {repo_path_obj}")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            observer.stop()
        observer.join()


if __name__ == "__main__":
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
    )
    logger.info("Logger configured for Real-Time Updater.")

    parser = argparse.ArgumentParser(
        description="Real-time graph updater for codebases."
    )
    parser.add_argument("repo_path", help="Path to the repository to watch.")
    parser.add_argument("--host", default="localhost", help="Memgraph host")
    parser.add_argument("--port", type=int, default=7687, help="Memgraph port")

    def positive_int(value: str) -> int:
        try:
            ivalue = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a valid integer"
            ) from exc
        if ivalue < 1:
            raise argparse.ArgumentTypeError(
                f"{value!r} is not a valid positive integer"
            )
        return ivalue

    parser.add_argument(
        "--batch-size",
        type=positive_int,
        default=None,
        help="Number of buffered nodes/relationships before flushing to Memgraph",
    )
    args = parser.parse_args()

    start_watcher(args.repo_path, args.host, args.port, args.batch_size)
