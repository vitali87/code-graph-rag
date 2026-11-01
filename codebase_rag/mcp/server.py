from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import jsonschema
from loguru import logger
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

from codebase_rag import constants as cs
from codebase_rag import exceptions as ex
from codebase_rag import logs as lg
from codebase_rag import tool_errors as te
from codebase_rag.config import settings
from codebase_rag.mcp.tools import (
    MCPToolsRegistry,
    create_mcp_tools_registry,
    tool_annotations,
)
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import LazyCypherGenerator
from codebase_rag.types_defs import MCPHandlerType, MCPToolArguments
from codebase_rag.utils.path_utils import derive_project_name
from codebase_rag.vector_store import close_qdrant_client
from codebase_rag.workspaces import WorkspaceConfig, WorkspaceError, load_workspace

if TYPE_CHECKING:
    # starlette is a lazy dependency of the HTTP path only; its ASGI
    # types are needed solely for annotations
    from starlette.types import ASGIApp, Receive, Scope, Send


def setup_logging() -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level=cs.MCP_LOG_LEVEL_INFO,
        format=cs.MCP_LOG_FORMAT,
        backtrace=False,
        diagnose=False,
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
        raise ex.RepoPathError(te.MCP_PATH_NOT_EXISTS.format(path=project_root))

    if not project_root.is_dir():
        raise ex.RepoPathError(te.MCP_PATH_NOT_DIR.format(path=project_root))

    logger.info(lg.MCP_SERVER_ROOT_RESOLVED.format(path=project_root))
    return project_root


def get_workspace(name: str | None = None) -> WorkspaceConfig | None:
    """The workspace the server scopes to, from the flag or the environment.

    Issue #1494: the CLI could already query a workspace, the server could
    not load one, so its notion of which projects exist was never
    workspace-backed. A workspace that does not exist is a configuration
    error at start-up, the same class as a missing root.
    """
    chosen = name or os.environ.get(cs.MCPEnvVar.MCP_WORKSPACE)
    if not chosen:
        return None
    try:
        config = load_workspace(chosen)
    except WorkspaceError as e:
        raise ValueError(str(e)) from e
    logger.info(
        lg.MCP_SERVER_WORKSPACE.format(name=config.name, count=len(config.repos))
    )
    return config


def create_server(workspace: str | None = None) -> tuple[Server, MemgraphIngestor]:
    setup_logging()

    try:
        project_root = get_project_root()
        logger.info(lg.MCP_SERVER_USING_ROOT.format(path=project_root))
        workspace_config = get_workspace(workspace)
    except ValueError as e:
        logger.error(lg.MCP_SERVER_CONFIG_ERROR.format(error=e))
        raise

    # Fail at startup with the role-aware missing-key diagnostic rather than
    # letting the first tool call surface a wrapped provider error from deep
    # inside CypherGenerator or the orchestrator (issue #1125). Local
    # providers pass through the validator's existing exemption.
    try:
        settings.active_orchestrator_config.validate_api_key(cs.ModelRole.ORCHESTRATOR)
        settings.active_cypher_config.validate_api_key(cs.ModelRole.CYPHER)
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

    # Scope Cypher generation to this server's project (named exactly as
    # indexing names it) so queries don't bleed into other projects sharing
    # the database (issue #425).
    # A workspace names every project the server serves (issue #1494).
    active_projects = (
        workspace_config.project_names()
        if workspace_config is not None
        else [derive_project_name(project_root)]
    )
    # Built on the first natural-language query, not here: indexing and the
    # deterministic tools need no LLM, and an unreachable provider must not
    # keep them from starting (issue #2518).
    cypher_generator = LazyCypherGenerator(active_projects=active_projects)

    tools = create_mcp_tools_registry(
        project_root=str(project_root),
        ingestor=ingestor,
        cypher_gen=cypher_generator,
        workspace=workspace_config,
    )

    logger.info(lg.MCP_SERVER_INIT_SUCCESS)

    def _create_error_content(message: str) -> CallToolResult:
        return _failed_call(te.failure(message))

    async def list_tools(
        ctx: ServerRequestContext, params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name=schema.name,
                    description=schema.description,
                    input_schema={**schema.inputSchema},
                    annotations=tool_annotations(schema.name),
                )
                for schema in tools.get_tool_schemas()
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext, params: CallToolRequestParams
    ) -> CallToolResult:
        name = params.name
        arguments: MCPToolArguments = params.arguments or {}
        logger.info(lg.MCP_SERVER_CALLING_TOOL.format(name=name))

        try:
            handler_info = _callable_handler(tools, name)
            if isinstance(handler_info, str):
                return _create_error_content(handler_info)

            handler, returns_json = handler_info

            invalid = _invalid_arguments(tools, name, arguments)
            if invalid is not None:
                return _failed_call(invalid)

            result = await handler(**arguments)

            if isinstance(result, te.ToolFailure):
                return _failed_call(result)
            if returns_json:
                result_text = json.dumps(result, indent=cs.MCP_JSON_INDENT)
            else:
                result_text = str(result)

            return CallToolResult(
                content=[TextContent(type=cs.MCP_CONTENT_TYPE_TEXT, text=result_text)],
                is_error=returns_json and _reports_failure(result),
            )

        except Exception as e:
            error_msg = cs.MCP_TOOL_EXEC_ERROR.format(name=name, error=e)
            logger.exception(lg.MCP_SERVER_TOOL_ERROR.format(name=name, error=e))
            return _create_error_content(error_msg)

    server = Server(
        cs.MCP_SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool
    )
    return server, ingestor


def _callable_handler(
    tools: MCPToolsRegistry, name: str
) -> tuple[MCPHandlerType, bool] | str:
    """Tool `name`'s handler and whether it returns JSON, or why it cannot
    be called on this server."""
    handler_info = tools.get_tool_handler(name)
    if not handler_info:
        logger.error(lg.MCP_SERVER_UNKNOWN_TOOL.format(name=name))
        return cs.MCP_UNKNOWN_TOOL_ERROR.format(name=name)
    # A client can call a tool it was never listed, so read-only mode
    # refuses here too, before the handler runs (issue #3458).
    if not tools.serves(name):
        logger.warning(lg.MCP_SERVER_READ_ONLY_REFUSED.format(name=name))
        return cs.MCP_READ_ONLY_REFUSED.format(name=name)
    return handler_info


def _invalid_arguments(
    tools: MCPToolsRegistry, name: str, arguments: MCPToolArguments
) -> str | None:
    # mcp 1.x checked the arguments against the listed input schema before a
    # handler ran; 2.x leaves that to the server, and the schemas' bounds
    # (a non-negative `read_file` offset, say) are only enforced here.
    for schema in tools.get_tool_schemas():
        if schema.name == name:
            try:
                jsonschema.validate(instance=arguments, schema={**schema.inputSchema})
            except jsonschema.ValidationError as e:
                return cs.MCP_INPUT_VALIDATION_ERROR.format(error=e.message)
    return None


def _reports_failure(result: object) -> bool:
    """Whether a JSON tool result is a refusal.

    A JSON tool refuses with an `error`, alone (an unknown project or name, a
    partial graph) or beside empty data: `reingest`'s `reparsed: []`, the
    sites a refused `rename` lists, `delete_project`'s `success: false`
    (issues #2802, #2785). One that reports work done beside an error, such
    as an applied rename reporting a marker it could not clear, did its work.
    """
    if not isinstance(result, dict) or not result.get(cs.DICT_KEY_ERROR):
        return False
    return not (
        result.get(cs.DICT_KEY_APPLIED) is True
        or result.get(cs.DICT_KEY_SUCCESS) is True
    )


def _failed_call(message: str) -> CallToolResult:
    # A failure the server or a tool detected must say so in the protocol
    # with `isError: true`, not only in its prose (issue #2650).
    return CallToolResult(
        content=[TextContent(type=cs.MCP_CONTENT_TYPE_TEXT, text=message)],
        is_error=True,
    )


@contextlib.contextmanager
def _service_lifecycle(ingestor: MemgraphIngestor) -> Generator[None]:
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


async def serve_stdio(workspace: str | None = None) -> None:
    logger.info(lg.MCP_SERVER_STARTING)

    server, ingestor = create_server(workspace)
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
    # The StreamableHTTP endpoint's only protection is the bearer token;
    # refusing a non-loopback bind without one makes accidental network
    # exposure of the unauthenticated transport impossible (follow-up to
    # #808: the loopback default protects default deployments, this
    # protects intentional remote ones).
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
        # RFC 7235: the SCHEME compares case-insensitively (not secret, so
        # an ordinary compare is fine); only the token value needs
        # constant-time compare_digest
        authorized = False
        if provided is not None:
            scheme, _, credential = provided.partition(b" ")
            authorized = scheme.lower() == b"bearer" and secrets.compare_digest(
                credential, token_bytes
            )
        if not authorized:
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
    workspace: str | None = None,
) -> None:
    import uvicorn
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.routing import Mount

    auth_token = settings.MCP_HTTP_AUTH_TOKEN
    _validate_http_exposure(host, auth_token)

    logger.info(lg.MCP_HTTP_SERVER_STARTING.format(host=host, port=port))

    server, ingestor = create_server(workspace)

    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=False,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            logger.info(lg.MCP_HTTP_SERVER_READY.format(host=host, port=port))
            yield

    # With a token, bearer auth fronts the mount even on loopback
    # (defense in depth for shared hosts); without one the exposure
    # guard above already confined the bind.
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
    # Connected before uvicorn starts, as stdio connects before its
    # transport: uvicorn logs a failure inside the lifespan and returns, so
    # the command exited 0 for a server that never started (PR 2504 review).
    with _service_lifecycle(ingestor):
        await uvicorn_server.serve()


if __name__ == "__main__":
    import asyncio

    asyncio.run(serve_stdio())
