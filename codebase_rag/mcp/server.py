"""MCP server implementation for code-graph-rag.

This module provides the main MCP server that exposes code-graph-rag's
capabilities via the Model Context Protocol.
"""

import os
import sys
from pathlib import Path

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from codebase_rag.config import settings
from codebase_rag.mcp.tools import create_mcp_tools_registry
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator


def setup_logging() -> None:
    """Configure logging to stderr for MCP stdio transport."""
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    )


def get_project_root() -> Path:
    """Get the project root from environment or settings.

    Returns:
        Path to the target repository

    Raises:
        ValueError: If TARGET_REPO_PATH is not set or invalid
    """
    # Try environment variable first, then fallback to settings
    repo_path = os.environ.get("TARGET_REPO_PATH", settings.TARGET_REPO_PATH)

    if not repo_path:
        raise ValueError(
            "TARGET_REPO_PATH environment variable must be set to the target repository path"
        )

    project_root = Path(repo_path).resolve()

    if not project_root.exists():
        raise ValueError(f"Target repository path does not exist: {project_root}")

    if not project_root.is_dir():
        raise ValueError(f"Target repository path is not a directory: {project_root}")

    return project_root


def create_server() -> tuple[Server, MemgraphIngestor]:
    """Create and configure the MCP server.

    Returns:
        Tuple of (configured MCP server instance, MemgraphIngestor instance)
    """
    setup_logging()

    # Get project root
    try:
        project_root = get_project_root()
        logger.info(f"[GraphCode MCP] Using project root: {project_root}")
    except ValueError as e:
        logger.error(f"[GraphCode MCP] Configuration error: {e}")
        raise

    # Initialize services
    logger.info("[GraphCode MCP] Initializing services...")

    ingestor = MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=settings.MEMGRAPH_BATCH_SIZE,
    )

    # CypherGenerator gets config from settings automatically
    cypher_generator = CypherGenerator()

    # Create tools registry
    tools = create_mcp_tools_registry(
        project_root=str(project_root),
        ingestor=ingestor,
        cypher_gen=cypher_generator,
    )

    logger.info("[GraphCode MCP] Services initialized successfully")

    # Create MCP server
    server = Server("graph-code")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        """List all available MCP tools."""
        return [
            Tool(
                name="index_repository",
                description="Parse and ingest the repository into the Memgraph knowledge graph. "
                "This builds a comprehensive graph of functions, classes, dependencies, and relationships.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            ),
            Tool(
                name="query_code_graph",
                description="Query the codebase knowledge graph using natural language. "
                "Ask questions like 'What functions call UserService.create_user?' or "
                "'Show me all classes that implement the Repository interface'.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "natural_language_query": {
                            "type": "string",
                            "description": "Your question in plain English about the codebase",
                        }
                    },
                    "required": ["natural_language_query"],
                },
            ),
            Tool(
                name="get_code_snippet",
                description="Retrieve source code for a function, class, or method by its qualified name. "
                "Returns the source code, file path, line numbers, and docstring.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "qualified_name": {
                            "type": "string",
                            "description": "Fully qualified name (e.g., 'app.services.UserService.create_user')",
                        }
                    },
                    "required": ["qualified_name"],
                },
            ),
            Tool(
                name="surgical_replace_code",
                description="Surgically replace an exact code block in a file using diff-match-patch. "
                "Only modifies the exact target block, leaving the rest unchanged.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file from project root",
                        },
                        "target_code": {
                            "type": "string",
                            "description": "Exact code block to replace",
                        },
                        "replacement_code": {
                            "type": "string",
                            "description": "New code to insert",
                        },
                    },
                    "required": ["file_path", "target_code", "replacement_code"],
                },
            ),
            Tool(
                name="read_file",
                description="Read the contents of a file from the project. Supports pagination for large files.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file from project root",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Line number to start reading from (0-based, optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to read (optional)",
                        },
                    },
                    "required": ["file_path"],
                },
            ),
            Tool(
                name="write_file",
                description="Write content to a file, creating it if it doesn't exist.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative path to the file from project root",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file",
                        },
                    },
                    "required": ["file_path", "content"],
                },
            ),
            Tool(
                name="list_directory",
                description="List contents of a directory in the project.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "directory_path": {
                            "type": "string",
                            "description": "Relative path to directory from project root (default: '.')",
                            "default": ".",
                        }
                    },
                    "required": [],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle tool execution requests."""
        import json

        logger.info(f"[GraphCode MCP] Calling tool: {name}")

        try:
            if name == "index_repository":
                result_text = await tools.index_repository()
                return [TextContent(type="text", text=result_text)]

            elif name == "query_code_graph":
                natural_language_query = arguments.get("natural_language_query", "")
                result_dict = await tools.query_code_graph(natural_language_query)
                return [
                    TextContent(type="text", text=json.dumps(result_dict, indent=2))
                ]

            elif name == "get_code_snippet":
                qualified_name = arguments.get("qualified_name", "")
                result_dict = await tools.get_code_snippet(qualified_name)
                return [
                    TextContent(type="text", text=json.dumps(result_dict, indent=2))
                ]

            elif name == "surgical_replace_code":
                file_path = arguments.get("file_path", "")
                target_code = arguments.get("target_code", "")
                replacement_code = arguments.get("replacement_code", "")
                result_text = await tools.surgical_replace_code(
                    file_path, target_code, replacement_code
                )
                return [TextContent(type="text", text=result_text)]

            elif name == "read_file":
                file_path = arguments.get("file_path", "")
                offset = arguments.get("offset")
                limit = arguments.get("limit")
                result_text = await tools.read_file(
                    file_path, offset=offset, limit=limit
                )
                return [TextContent(type="text", text=result_text)]

            elif name == "write_file":
                file_path = arguments.get("file_path", "")
                content = arguments.get("content", "")
                result_text = await tools.write_file(file_path, content)
                return [TextContent(type="text", text=result_text)]

            elif name == "list_directory":
                directory_path = arguments.get("directory_path", ".")
                result_text = await tools.list_directory(directory_path)
                return [TextContent(type="text", text=result_text)]

            else:
                error_msg = f"Unknown tool: {name}"
                logger.error(f"[GraphCode MCP] {error_msg}")
                return [TextContent(type="text", text=f"Error: {error_msg}")]

        except Exception as e:
            error_msg = f"Error executing tool '{name}': {str(e)}"
            logger.error(f"[GraphCode MCP] {error_msg}")
            return [TextContent(type="text", text=f"Error: {error_msg}")]

    return server, ingestor


async def main() -> None:
    """Main entry point for the MCP server."""
    logger.info("[GraphCode MCP] Starting MCP server...")

    server, ingestor = create_server()
    logger.info("[GraphCode MCP] Server created, starting stdio transport...")

    # Use context manager to ensure proper cleanup of Memgraph connection
    with ingestor:
        logger.info(
            f"[GraphCode MCP] Connected to Memgraph at {settings.MEMGRAPH_HOST}:{settings.MEMGRAPH_PORT}"
        )
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream, server.create_initialization_options()
                )
        except Exception as e:
            logger.error(f"[GraphCode MCP] Fatal error: {e}")
            raise
        finally:
            logger.info("[GraphCode MCP] Shutting down server...")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
