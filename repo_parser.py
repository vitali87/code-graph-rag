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

            logger.info("STEP 4: Initializing and running GraphUpdater...")
            updater = GraphUpdater(ingestor, repo_path)
            updater.run()  # This single call now orchestrates everything
            logger.info("GraphUpdater finished successfully.")

    except Exception as e:
        logger.error(f"!!! An unexpected error occurred in main: {e}", exc_info=True)

    logger.info("\nFinished processing repository.")


if __name__ == "__main__":
    main()
