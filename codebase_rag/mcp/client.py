"""MCP client for querying the code graph via the MCP server.

This module provides a simple CLI client that connects to the MCP server
and executes the ask_code_graph tool with a provided question.
"""

import asyncio
import json
import sys
from typing import Any

import typer
from mcp import ClientSession
from mcp.client.stdio import stdio_client

app = typer.Typer()


async def query_mcp_server(question: str) -> dict[str, Any]:
    """Query the MCP server with a question.

    Args:
        question: The question to ask about the codebase

    Returns:
        Dictionary with the response from the server
    """
    async with stdio_client() as (read, write):
        async with ClientSession(read, write) as session:
            # Initialize the session
            await session.initialize()

            # Call the ask_code_graph tool
            result = await session.call_tool("ask_code_graph", {"question": question})

            # Extract the response text
            if result.content:
                response_text = result.content[0].text
                # Parse JSON response
                try:
                    parsed = json.loads(response_text)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"output": str(parsed)}
                except json.JSONDecodeError:
                    return {"output": response_text}
            return {"output": "No response from server"}


@app.command()
def main(
    question: str = typer.Option(
        ..., "--question", "-q", help="Question to ask about the codebase"
    ),
) -> None:
    """Query the code graph via MCP server.

    Example:
        python -m codebase_rag.mcp.client --question "What functions call UserService.create_user?"
    """
    try:
        # Run the async query
        result = asyncio.run(query_mcp_server(question))

        # Print only the output (clean for scripting)
        if isinstance(result, dict) and "output" in result:
            print(result["output"])
        else:
            print(json.dumps(result))

    except Exception as e:
        # Print error to stderr and exit with error code
        print(f"Error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    app()
