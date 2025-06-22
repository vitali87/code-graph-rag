import asyncio
import argparse
import sys
from .config import settings
from .graph_updater import MemgraphIngestor
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.codebase_query import create_query_tool
from .tools.code_retrieval import create_code_retrieval_tool, CodeRetriever
from .tools.file_reader import create_file_reader_tool, FileReader

from loguru import logger


async def main(target_repo_path: str = None):
    """Initializes services and runs the main application loop."""

    logger.remove()
    logger.add(sys.stdout, format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {message}")

    repo_path = target_repo_path or settings.TARGET_REPO_PATH
    logger.info(f"Codebase RAG CLI - Using Model: {settings.GEMINI_MODEL_ID}")
    logger.info(f"Target Repository: {repo_path}")

    # Use a single MemgraphIngestor instance within a context manager
    with MemgraphIngestor(
        host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
    ) as ingestor:
        logger.info("Database connection established.")
        logger.info(
            "Ask questions about your codebase graph. Type 'exit' or 'quit' to end."
        )
        logger.info("-" * 50)

        # 1. Initialize services
        cypher_generator = CypherGenerator()
        code_retriever = CodeRetriever(project_root=repo_path, ingestor=ingestor)
        file_reader = FileReader(project_root=repo_path)

        # 2. Create tools, injecting the *same* ingestor instance
        query_tool = create_query_tool(ingestor, cypher_generator)
        code_tool = create_code_retrieval_tool(
            code_retriever
        )  # This tool needs its own rewrite next
        file_reader_tool = create_file_reader_tool(file_reader)

        # 3. Create the main agent
        rag_agent = create_rag_orchestrator(
            tools=[query_tool, code_tool, file_reader_tool]
        )

        message_history = []

        # 4. Main loop
        while True:
            try:
                user_input = await asyncio.to_thread(input, "\nAsk a question: ")
                if user_input.lower() in ["exit", "quit"]:
                    break
                if not user_input.strip():
                    continue

                response = await rag_agent.run(
                    user_input, message_history=message_history
                )
                logger.info(f"\nFinal Answer:\n{response.output}")
                logger.info("\n" + "=" * 70)

                message_history.extend(response.new_messages())

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"\nAn unexpected error occurred: {e}", exc_info=True)

    logger.info("\nExiting...")


def start():
    parser = argparse.ArgumentParser(description="Codebase RAG CLI")
    parser.add_argument(
        "--repo-path", help="Path to the target repository for code retrieval"
    )
    parser.add_argument(
        "--update-graph",
        action="store_true",
        help="Update the knowledge graph by parsing the repository",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Clean the database before updating (use when adding first repo)",
    )
    args = parser.parse_args()

    # If update-graph flag is provided, run graph updater instead of RAG CLI
    if args.update_graph:
        from pathlib import Path
        from .graph_updater import GraphUpdater, MemgraphIngestor

        repo_path = Path(args.repo_path or settings.TARGET_REPO_PATH)
        logger.info(f"Updating knowledge graph for: {repo_path}")

        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
        ) as ingestor:
            if args.clean:
                logger.info("Cleaning database...")
                ingestor.clean_database()
            ingestor.ensure_constraints()
            updater = GraphUpdater(ingestor, repo_path)
            updater.run()

        logger.info("Graph update completed!")
        return

    try:
        asyncio.run(main(target_repo_path=args.repo_path))
    except KeyboardInterrupt:
        logger.error("\nApplication terminated by user.")
    except ValueError as e:  # Catch config errors from startup
        logger.error(f"Startup Error: {e}")


if __name__ == "__main__":
    start()
