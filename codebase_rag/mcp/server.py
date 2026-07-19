import contextlib
import json
import os
import sys
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from pathlib import Path
from typing import Any

from loguru import logger
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from codebase_rag import constants as cs
from codebase_rag import logs as lg
from codebase_rag import tool_errors as te
from codebase_rag.config import settings
from codebase_rag.mcp.tools import create_mcp_tools_registry
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator
from codebase_rag.types_defs import MCPToolArguments
from codebase_rag.vector_store import close_qdrant_client

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=cs.MCP_LOG_LEVEL_INFO,
        format=cs.MCP_LOG_FORMAT,
    )


def get_project_root() -> Path:
    repo_path: str | None = (
        os.environ.get(cs.MCPEnvVar.TARGET_REPO_PATH) or settings.TARGET_REPO_PATH
    )

    if not repo_path:
        repo_path = os.environ.get(cs.MCPEnvVar.CLAUDE_PROJECT_ROOT) or os.environ.get(
            cs.MCPEnvVar.PWD
        )

        if repo_path:
            logger.info(lg.MCP_SERVER_INFERRED_ROOT.format(path=repo_path))
        else:
            repo_path = str(Path.cwd())
            logger.info(lg.MCP_SERVER_NO_ROOT.format(path=repo_path))

    project_root = Path(repo_path).resolve()

    if not project_root.exists():
        raise ValueError(te.MCP_PATH_NOT_EXISTS.format(path=project_root))

    if not project_root.is_dir():
        raise ValueError(te.MCP_PATH_NOT_DIR.format(path=project_root))

    logger.info(lg.MCP_SERVER_ROOT_RESOLVED.format(path=project_root))
    return project_root


def create_server() -> tuple[Server, MemgraphIngestor]:
    setup_logging()

    try:
        project_root = get_project_root()
        logger.info(lg.MCP_SERVER_USING_ROOT.format(path=project_root))
    except ValueError as e:
        logger.error(lg.MCP_SERVER_CONFIG_ERROR.format(error=e))
        raise

    logger.info(lg.MCP_SERVER_INIT_SERVICES)

    ingestor = MemgraphIngestor(
        host=settings.MEMGRAPH_HOST,
        port=settings.MEMGRAPH_PORT,
        batch_size=settings.MEMGRAPH_BATCH_SIZE,
        username=settings.MEMGRAPH_USERNAME,
        password=settings.MEMGRAPH_PASSWORD,
    )

    cypher_generator = CypherGenerator()

    tools = create_mcp_tools_registry(
        project_root=str(project_root),
        ingestor=ingestor,
        cypher_gen=cypher_generator,
    )

    logger.info(lg.MCP_SERVER_INIT_SUCCESS)

    server = Server(cs.MCP_SERVER_NAME)

    def _create_error_content(message: str) -> list[TextContent]:
        return [
            TextContent(
                type=cs.MCP_CONTENT_TYPE_TEXT,
                text=te.ERROR_WRAPPER.format(message=message),
            )
        ]

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        schemas = tools.get_tool_schemas()
        return [
            Tool(
                name=schema.name,
                description=schema.description,
                inputSchema={**schema.inputSchema},
            )
            for schema in schemas
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: MCPToolArguments) -> list[TextContent]:
        logger.info(lg.MCP_SERVER_CALLING_TOOL.format(name=name))

        try:
            handler_info = tools.get_tool_handler(name)
            if not handler_info:
                error_msg = cs.MCP_UNKNOWN_TOOL_ERROR.format(name=name)
                logger.error(lg.MCP_SERVER_UNKNOWN_TOOL.format(name=name))
                return _create_error_content(error_msg)

            handler, returns_json = handler_info

            result = await handler(**arguments)

            if returns_json:
                result_text = json.dumps(result, indent=cs.MCP_JSON_INDENT)
            else:
                result_text = str(result)

            return [TextContent(type=cs.MCP_CONTENT_TYPE_TEXT, text=result_text)]

        except Exception as e:
            error_msg = cs.MCP_TOOL_EXEC_ERROR.format(name=name, error=e)
            logger.exception(lg.MCP_SERVER_TOOL_ERROR.format(name=name, error=e))
            return _create_error_content(error_msg)

    return server, ingestor


@contextlib.contextmanager
def _service_lifecycle(ingestor: MemgraphIngestor) -> Iterator[None]:
    """Manage shared service lifetimes for the MCP server.

    Opens the Memgraph ingestor connection and releases the vector store client
    on shutdown, so a CLI indexing run can reuse local resources once the server
    stops.
    """
    try:
        with ingestor:
            logger.info(
                lg.MCP_SERVER_CONNECTED.format(
                    host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT
                )
            )
            yield
    finally:
        close_qdrant_client()


async def serve_stdio() -> None:
    logger.info(lg.MCP_SERVER_STARTING)

    server, ingestor = create_server()
    logger.info(lg.MCP_SERVER_CREATED)

    with _service_lifecycle(ingestor):
        try:
            async with stdio_server() as (read_stream, write_stream):
                await server.run(
                    read_stream, write_stream, server.create_initialization_options()
                )
        except Exception as e:
            logger.error(lg.MCP_SERVER_FATAL_ERROR.format(error=e))
            raise
        finally:
            logger.info(lg.MCP_SERVER_SHUTDOWN)


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _validate_http_exposure(host: str, auth_token: str | None) -> None:
    # (H) The StreamableHTTP endpoint's only protection is the bearer token;
    # (H) refusing a non-loopback bind without one makes accidental network
    # (H) exposure of the unauthenticated transport impossible (follow-up to
    # (H) #808: the loopback default protects default deployments, this
    # (H) protects intentional remote ones).
    if host not in _LOOPBACK_HOSTS and not auth_token:
        raise ValueError(lg.MCP_HTTP_EXPOSURE_REFUSED.format(host=host))


def _require_bearer_auth(app: ASGIApp, auth_token: str) -> ASGIApp:
    import secrets

    token_bytes = auth_token.encode(cs.ENCODING_UTF8)

    async def guarded(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        provided: bytes | None = None
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                provided = value
                break
        expected = b"Bearer " + token_bytes
        # (H) compare_digest keeps the check constant-time; comparing against
        # (H) the full "Bearer <token>" bytes covers scheme and value in one
        # (H) comparison without an early-exit prefix check
        if provided is None or not secrets.compare_digest(provided, expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        await app(scope, receive, send)

    return guarded


async def serve_http(
    host: str = settings.MCP_HTTP_HOST,
    port: int = settings.MCP_HTTP_PORT,
) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    auth_token = settings.MCP_HTTP_AUTH_TOKEN
    _validate_http_exposure(host, auth_token)

    logger.info(lg.MCP_HTTP_SERVER_STARTING.format(host=host, port=port))

    server, ingestor = create_server()

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        with _service_lifecycle(ingestor):
            async with session_manager.run():
                logger.info(lg.MCP_HTTP_SERVER_READY.format(host=host, port=port))
                yield

    # (H) With a token configured, bearer auth fronts the mount even on
    # (H) loopback (defense in depth for shared hosts); without one, the
    # (H) exposure guard above already confined the bind to loopback.
    endpoint = session_manager.handle_request
    if auth_token:
        endpoint = _require_bearer_auth(endpoint, auth_token)

    starlette_app = Starlette(
        routes=[
            Mount(settings.MCP_HTTP_ENDPOINT_PATH, app=endpoint),
        ],
        lifespan=lifespan,
    )

    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    uvicorn_server = uvicorn.Server(config)
    await uvicorn_server.serve()


if __name__ == "__main__":
    import asyncio

    asyncio.run(serve_stdio())
