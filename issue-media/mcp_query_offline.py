#!/usr/bin/env python3
"""Ask query_code_graph one question through cgr mcp-server; the Cypher model is a local stub."""
import asyncio, json, os
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    params = StdioServerParameters(command="cgr", args=["mcp-server"], env=dict(os.environ))
    async with stdio_client(params, errlog=open(os.devnull, "w")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("query_code_graph", {"natural_language_query": "which functions exist?"})
            body = json.loads(res.content[0].text)
            print("query_used:", body["query_used"])
            print("results:   ", body["results"])
            print("summary:   ", body["summary"][:170])

asyncio.run(main())
