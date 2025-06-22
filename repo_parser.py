import argparse
import os
import sys
from pathlib import Path
from loguru import logger

from codebase_rag.graph_updater import MemgraphIngestor, GraphUpdater


def main() -> None:
    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
    )
    logger.info("Logger configured.")
    arg_parser = argparse.ArgumentParser(
        description="Parse a Python repository and ingest its structure into Memgraph."
    )
    arg_parser.add_argument(
        "repo_path", help="The absolute path to the Python repository to analyze."
    )
    arg_parser.add_argument(
        "--host", default=os.getenv("MEMGRAPH_HOST", "localhost"), help="Memgraph host"
    )
    arg_parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MEMGRAPH_PORT", 7687)),
        help="Memgraph port",
    )
    arg_parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete all existing data from the database before parsing.",
    )
    arg_parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of write operations to batch together.",
    )
    args = arg_parser.parse_args()

    repo_path = Path(args.repo_path).resolve()
    if not repo_path.is_dir():
        logger.error(f"!!! ERROR: Repository path '{repo_path}' does not exist.")
        return

    try:
        logger.info("--- Initializing ---")
        with MemgraphIngestor(
            host=args.host, port=args.port, batch_size=args.batch_size
        ) as ingestor:
            logger.info("STEP 1: MemgraphIngestor context entered.")

            if args.clean:
                logger.info("STEP 2: Cleaning database...")
                ingestor.clean_database()

            logger.info("STEP 3: Ensuring constraints...")
            ingestor.ensure_constraints()

            logger.info("STEP 4: Initializing GraphUpdater...")
            # Use a clear variable name 'updater' instead of 'parser'
            updater = GraphUpdater(ingestor, repo_path)
            logger.info("GraphUpdater initialized successfully.")

            logger.info(f"--- Starting repository scan of {repo_path} ---")

            # Walk the repo and parse each file
            ignore_dirs = {
                ".git",
                "venv",
                ".venv",
                "__pycache__",
                "node_modules",
                "build",
                "dist",
                ".eggs",
            }
            for root, dirs, files in os.walk(repo_path, topdown=True):
                dirs[:] = [d for d in dirs if d not in ignore_dirs]

                for file_name in files:
                    file_path = Path(root) / file_name
                    updater.parse_and_ingest_file(file_path)

            logger.info("--- Repository scan complete. Flushing final data... ---")
            # The flush is now inside the with block to ensure connection is active
            ingestor.flush_all()

    except Exception as e:
        logger.error(f"!!! An unexpected error occurred in main: {e}", exc_info=True)
        # exc_info=True will print the full traceback for debugging

    logger.info("\nFinished processing repository.")


if __name__ == "__main__":
    main()
