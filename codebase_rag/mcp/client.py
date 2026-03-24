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
    with open(os.devnull, "w") as devnull:
        server_params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codebase_rag.cli", "mcp-server"],
        )

        async with stdio_client(server=server_params, errlog=devnull) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("ask_agent", {"question": question})

                if result.content:
                    response_text = result.content[0].text
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
    try:
        result = asyncio.run(query_mcp_server(question))
        if isinstance(result, dict) and "output" in result:
            print(result["output"])  # noqa: T201
        else:
            print(json.dumps(result))  # noqa: T201
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    app()
