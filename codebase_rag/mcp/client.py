"""MCP client for querying the code graph via the MCP server.



This module provides a simple CLI client that connects to the MCP server

and executes the ask_agent tool with a provided question.

"""

import asyncio
import json
import os
import sys
from typing import Any

import typer
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

app = typer.Typer()


async def query_mcp_server(question: str) -> dict[str, Any]:
    """Query the MCP server with a question.



    Args:

        question: The question to ask about the codebase



    Returns:

        Dictionary with the response from the server

    """

    # Start the MCP server as a subprocess with stderr redirected to /dev/null

    # This suppresses all server logs while keeping stdout/stdin for MCP communication

    with open(os.devnull, "w") as devnull:
        server_params = StdioServerParameters(
            command="python",
            args=["-m", "codebase_rag.main", "mcp-server"],
        )

        async with stdio_client(server=server_params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                # Initialize the session

                await session.initialize()

                # Call the ask_agent tool

                result = await session.call_tool("ask_agent", {"question": question})

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
        ..., "--ask-agent", "-a", help="Question to ask about the codebase"
    ),
) -> None:
    """Query the code graph via MCP server.



    Example:

        python -m codebase_rag.mcp.client --ask-agent "What functions call UserService.create_user?"

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
