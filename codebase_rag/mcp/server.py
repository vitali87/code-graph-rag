import json
import os
from pathlib import Path

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from codebase_rag.config import settings
from codebase_rag.mcp.tools import create_mcp_tools_registry
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator


def setup_logging(enable_logging: bool = False) -> None:
    """Configure logging for MCP stdio transport.

    By default, logging is disabled to prevent token waste in LLM context.
    Can be enabled via environment variable MCP_ENABLE_LOGGING=1 for debugging.

    When enabled, logs are written to a file to avoid polluting STDIO transport.
    The log file path can be configured via MCP_LOG_FILE environment variable.

    Args:
        enable_logging: Whether to enable logging output. Defaults to False.
                       Can also be controlled via MCP_ENABLE_LOGGING environment variable.
    """
    logger.remove()  # Remove default handler

    # Check environment variable to override enable_logging parameter
    env_enable = os.environ.get("MCP_ENABLE_LOGGING", "").lower() in (
        "1",
        "true",
        "yes",
    )
    should_enable = enable_logging or env_enable

    if should_enable:
        # Get log file path from environment or use default
        log_file = os.environ.get("MCP_LOG_FILE")
        if not log_file:
            # Use ~/.cache/code-graph-rag/mcp.log as default
            cache_dir = Path.home() / ".cache" / "code-graph-rag"
            cache_dir.mkdir(parents=True, exist_ok=True)
            log_file = str(cache_dir / "mcp.log")

        # Ensure log file directory exists
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # Add file handler - logs go to file, not STDERR/STDOUT
        logger.add(
            log_file,
            level="INFO",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
            colorize=False,  # Disable ANSI color codes
            rotation="10 MB",  # Rotate when file reaches 10MB
            retention="7 days",  # Keep logs for 7 days
        )
    else:
        # Disable all logging by default for MCP mode
        logger.disable("codebase_rag")


def get_project_root() -> Path:
    """Get the project root from environment, settings, or parent process working directory.

    Priority order:
    1. TARGET_REPO_PATH environment variable (explicit configuration)
    2. TARGET_REPO_PATH from settings
    3. CLAUDE_PROJECT_ROOT environment variable (set by Claude Code)
    4. PWD environment variable (inherited from parent process/shell)
    5. Current working directory (fallback)

    Returns:
        Path to the target repository

    Raises:
        ValueError: If the resolved path is invalid
    """
    # Try explicit TARGET_REPO_PATH first
    repo_path: str | None = (
        os.environ.get("TARGET_REPO_PATH") or settings.TARGET_REPO_PATH
    )

    if not repo_path:
        # Try Claude Code project root env var
        repo_path = os.environ.get("CLAUDE_PROJECT_ROOT")

        if not repo_path:
            # Try PWD from parent process (often set by shells and reflects where command was run)
            repo_path = os.environ.get("PWD")

        if repo_path:
            logger.info(f"[GraphCode MCP] Using inferred project root: {repo_path}")
        else:
            # Last resort: current working directory
            repo_path = str(Path.cwd())
            logger.info(
                f"[GraphCode MCP] No project root configured, using current directory: {repo_path}"
            )

    project_root = Path(repo_path).resolve()

    if not project_root.exists():
        raise ValueError(f"Target repository path does not exist: {project_root}")

    if not project_root.is_dir():
        raise ValueError(f"Target repository path is not a directory: {project_root}")

    logger.info(f"[GraphCode MCP] Project root resolved to: {project_root}")
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
        """List all available MCP tools.

        Tool schemas are dynamically generated from the MCPToolsRegistry,
        ensuring consistency between tool definitions and handlers.
        """
        schemas = tools.get_tool_schemas()
        return [
            Tool(
                name=schema["name"],
                description=schema["description"],
                inputSchema=schema["inputSchema"],
            )
            for schema in schemas
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        """Handle tool execution requests.

        Tool handlers are dynamically resolved from the MCPToolsRegistry,
        ensuring consistency with tool definitions.

        Logging is suppressed during tool execution to prevent token waste in LLM context.
        """
        import io
        from contextlib import redirect_stderr, redirect_stdout

        try:
            # Resolve handler from registry
            handler_info = tools.get_tool_handler(name)
            if not handler_info:
                error_msg = "Unknown tool"
                return [TextContent(type="text", text=f"Error: {error_msg}")]

            handler, returns_json = handler_info

            # Suppress all logging output during tool execution
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                logger.disable("codebase_rag")
                try:
                    # Call handler with unpacked arguments
                    result = await handler(**arguments)

                    # Format result based on output type
                    if returns_json:
                        result_text = json.dumps(result, indent=2)
                    else:
                        result_text = str(result)

                    return [TextContent(type="text", text=result_text)]
                finally:
                    logger.enable("codebase_rag")

        except Exception:
            # Fail silently without logging or printing error details
            return [
                TextContent(
                    type="text", text="Error: There was an error executing the tool"
                )
            ]

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
