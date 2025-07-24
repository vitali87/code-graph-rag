import argparse
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.language_config import get_language_config
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor


class CodeChangeEventHandler(FileSystemEventHandler):
    """Handles file system events and updates the graph accordingly."""

    def __init__(self, updater: GraphUpdater):
        self.updater = updater
        # Expanded ignore patterns to include common temporary/junk files
        self.ignore_patterns = {".git", "__pycache__", ".venv", ".idea", ".vscode"}
        self.ignore_suffixes = {".tmp", "~"}
        logger.info("File watcher is now active.")

    def _is_relevant(self, path_str: str) -> bool:
        """Check if the file path is relevant for processing."""
        path = Path(path_str)
        if any(path.name.endswith(suffix) for suffix in self.ignore_suffixes):
            return False
        return not any(part in self.ignore_patterns for part in path.parts)

    def dispatch(self, event: Any) -> None:
        """A single dispatch method to handle all file system events."""
        if event.is_directory or not self._is_relevant(event.src_path):
            return

        path = Path(event.src_path)
        relative_path_str = str(path.relative_to(self.updater.repo_path))

        logger.warning(
            f"Change detected: {event.event_type} on {path}. Updating graph."
        )

        # --- Step 1: Delete all old data from the graph for this file ---
        # This provides a clean slate for the updated information.
        delete_query = "MATCH (m:Module {path: $path})-[*0..]->(c) DETACH DELETE m, c"
        self.updater.ingestor.execute_write(delete_query, {"path": relative_path_str})
        logger.debug(f"Ran deletion query for path: {relative_path_str}")

        # --- Step 2: Clear the specific in-memory state for the file ---
        # Crucial for preventing stale in-memory representations.
        self.updater.remove_file_from_state(path)

        # --- Step 3: Re-parse the file if it was modified or created ---
        # This rebuilds the in-memory state (AST, function registry) for the single file.
        if event.event_type in ["modified", "created"]:
            lang_config = get_language_config(path.suffix)
            if lang_config and lang_config.name in self.updater.parsers:
                self.updater.parse_and_ingest_file(path, lang_config.name)

        # --- Step 4: Re-process all function calls across the entire codebase ---
        # This is the key to fixing the "island" problem. It ensures that changes
        # in one file are correctly reflected in relationships from all other files.
        logger.info("Recalculating all function call relationships for consistency...")
        self.updater.ingestor.execute_write("MATCH ()-[r:CALLS]->() DELETE r")
        self.updater._process_function_calls()

        # --- Step 5: Flush all collected changes to the database ---
        self.updater.ingestor.flush_all()
        logger.success(f"Graph updated successfully for change in: {path.name}")


def start_watcher(repo_path: str, host: str, port: int) -> None:
    """Initializes the graph updater and starts the file system watcher."""
    repo_path_obj = Path(repo_path).resolve()
    parsers, queries = load_parsers()

    with MemgraphIngestor(host=host, port=port) as ingestor:
        updater = GraphUpdater(ingestor, repo_path_obj, parsers, queries)

        # --- Perform an initial full scan to build the complete context ---
        # This is essential for the real-time updates to have a valid baseline.
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
    args = parser.parse_args()

    start_watcher(args.repo_path, args.host, args.port)
