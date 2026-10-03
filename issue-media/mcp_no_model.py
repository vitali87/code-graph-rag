#!/usr/bin/env python3
"""Call the two LLM-backed MCP tools on a server with no model configured."""
import asyncio, json, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALLS = [
    ("query_code_graph", {"natural_language_query": "which functions call get_user?"}),
    ("ask_agent", {"question": "what does api.py expose?"}),
    ("callers", {"qualified_name": "x", "project": "no_such_project"}),
]

async def main():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CYPHER_", "ORCHESTRATOR_"))}
    params = StdioServerParameters(command="cgr", args=["mcp-server"], env=env)
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for name, args in CALLS:
                res = await s.call_tool(name, args)
                body = json.loads("".join(getattr(c, "text", "") for c in res.content))
                print(f"{name:17} isError={res.isError!s:5}  error: {body.get('error', '')[:95]}")

asyncio.run(main())
