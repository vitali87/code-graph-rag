import asyncio
from .config import settings
from .services.graph_db import memgraph_service
from .services.llm import CypherGenerator, create_rag_orchestrator
from .tools.codebase_query import create_query_tool

async def main():
    """Initializes services and runs the main application loop."""
    print(f"Codebase RAG CLI - Using Model: {settings.GEMINI_MODEL_ID}")
    print("Ask questions about your codebase graph. Type 'exit' or 'quit' to end.")
    print("-" * 50)

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
            print(f"\nFinal Answer:\n{response.output}")
            print("\n" + "="*70)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"\nAn unexpected error occurred: {e}")

    print("\nExiting...")

def start():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nApplication terminated by user.")
    except ValueError as e: # Catch config errors from startup
        print(f"Startup Error: {e}")


if __name__ == "__main__":
    start()