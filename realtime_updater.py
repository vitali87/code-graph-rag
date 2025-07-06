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
    def __init__(self, updater: GraphUpdater):
        self.updater = updater
        self.ignore_patterns = {".git", "__pycache__", ".venv"}
        logger.info("File watcher is now active.")

    def _is_relevant(self, path_str: str) -> bool:
        return not any(part in self.ignore_patterns for part in Path(path_str).parts)

    def dispatch(self, event: Any) -> None:
        """A single dispatch method to handle all events."""
        if not self._is_relevant(event.src_path) or event.is_directory:
            return

        path = Path(event.src_path)

        logger.warning(
            f"Change detected: {event.event_type} on {path}. Updating graph."
        )
        query = "MATCH (m:Module {path: $path})-[*0..]->(c) DETACH DELETE m, c"
        self.updater.ingestor.execute_write(
            query, {"path": str(path.relative_to(self.updater.repo_path))}
        )

        if event.event_type in ["modified", "created"]:
            lang_config = get_language_config(path.suffix)
            if lang_config and lang_config.name in self.updater.parsers:
                self.updater.parse_and_ingest_file(path, lang_config.name)

        self.updater.ingestor.flush_all()
        logger.success(f"Graph updated for: {path.name}")


def start_watcher(repo_path: str, host: str, port: int) -> None:
    repo_path_obj = Path(repo_path).resolve()

    parsers, queries = load_parsers()

    with MemgraphIngestor(host=host, port=port) as ingestor:
        updater = GraphUpdater(ingestor, repo_path_obj, parsers, queries)
        event_handler = CodeChangeEventHandler(updater)

        observer = Observer()
        # watchdog expects a string path, so we convert the Path object back to a string.
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
