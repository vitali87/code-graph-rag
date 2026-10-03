#!/usr/bin/env python3
"""Read the latin-1 module back through the MCP tools."""
import asyncio, json, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CALLS = [
    ("get_code_snippet", {"qualified_name": "latin56.menu.café"}),
    ("read_file", {"file_path": "menu.py"}),
]

async def main():
    params = StdioServerParameters(command="cgr", args=["mcp-server"], env=dict(os.environ))
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for name, args in CALLS:
                res = await s.call_tool(name, args)
                text = "".join(getattr(c, "text", "") for c in res.content).replace(os.getcwd() + "/", "")
                try:
                    body = json.loads(text)
                    text = f"found={body.get('found')} error_message={body.get('error_message')}"
                except ValueError:
                    pass
                print(f"{name:16} isError={res.isError!s:5} {text[:120]}")

asyncio.run(main())
