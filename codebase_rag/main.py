import asyncio
from .config import settings
from .services.graph_db import memgraph_service
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.codebase_query import create_query_tool
from loguru import logger

async def main():
    """Initializes services and runs the main application loop."""
    logger.info(f"Codebase RAG CLI - Using Model: {settings.GEMINI_MODEL_ID}")
    logger.info("Ask questions about your codebase graph. Type 'exit' or 'quit' to end.")
    logger.info("-" * 50)

    # 1. Initialize services
    cypher_generator = CypherGenerator()
    
    # 2. Create tools, injecting services as dependencies
    query_tool = create_query_tool(memgraph_service, cypher_generator)
    
    # 3. Create the main agent, injecting the tools
    rag_agent = create_rag_orchestrator(tools=[query_tool])
    
    # 4. Start the main loop
    while True:
        try:
            user_input = await asyncio.to_thread(input, "\nAsk a question: ")
            if user_input.lower() in ["exit", "quit"]:
                break
            if not user_input.strip():
                continue
            
            response = await rag_agent.run(user_input)
            logger.info(f"\nFinal Answer:\n{response.output}")
            logger.info("\n" + "="*70)

        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"\nAn unexpected error occurred: {e}")

    logger.info("\nExiting...")

def start():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.error("\nApplication terminated by user.")
    except ValueError as e: # Catch config errors from startup
        logger.error(f"Startup Error: {e}")


if __name__ == "__main__":
    start()