import itertools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from loguru import logger

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.services.llm import CypherGenerator
from codebase_rag.tools.code_retrieval import CodeRetriever, create_code_retrieval_tool
from codebase_rag.tools.codebase_query import create_query_tool
from codebase_rag.tools.directory_lister import (
    DirectoryLister,
    create_directory_lister_tool,
)
from codebase_rag.tools.file_editor import FileEditor, create_file_editor_tool
from codebase_rag.tools.file_reader import FileReader, create_file_reader_tool
from codebase_rag.tools.file_writer import FileWriter, create_file_writer_tool


@dataclass
class ToolMetadata:
    """Metadata for an MCP tool including schema and handler information."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]
    returns_json: bool


class MCPToolsRegistry:
    """Registry for all MCP tools with shared dependencies."""

    def __init__(
        self,
        project_root: str,
        ingestor: MemgraphIngestor,
        cypher_gen: CypherGenerator,
    ) -> None:
        """Initialize the MCP tools registry.

        Args:
            project_root: Path to the target repository
            ingestor: Memgraph ingestor instance
            cypher_gen: Cypher query generator instance
        """
        self.project_root = project_root
        self.ingestor = ingestor
        self.cypher_gen = cypher_gen

        # Load parsers for repository indexing
        self.parsers, self.queries = load_parsers()

        # Initialize service instances
        self.code_retriever = CodeRetriever(project_root, ingestor)
        self.file_editor = FileEditor(project_root=project_root)
        self.file_reader = FileReader(project_root=project_root)
        self.file_writer = FileWriter(project_root=project_root)
        self.directory_lister = DirectoryLister(project_root=project_root)

        # Create pydantic-ai tools - we'll call the underlying functions directly
        self._query_tool = create_query_tool(
            ingestor=ingestor, cypher_gen=cypher_gen, console=None
        )
        self._code_tool = create_code_retrieval_tool(code_retriever=self.code_retriever)
        self._file_editor_tool = create_file_editor_tool(file_editor=self.file_editor)
        self._file_reader_tool = create_file_reader_tool(file_reader=self.file_reader)
        self._file_writer_tool = create_file_writer_tool(file_writer=self.file_writer)
        self._directory_lister_tool = create_directory_lister_tool(
            directory_lister=self.directory_lister
        )

        # Build tool registry - single source of truth for all tool metadata
        self._tools: dict[str, ToolMetadata] = {
            "index_repository": ToolMetadata(
                name="index_repository",
                description="Parse and ingest the repository into the Memgraph knowledge graph. "
                "This builds a comprehensive graph of functions, classes, dependencies, and relationships.",
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                handler=self.index_repository,
                returns_json=False,
            ),
            "query_code_graph": ToolMetadata(
                name="query_code_graph",
                description="Query the codebase knowledge graph using natural language. "
                "Ask questions like 'What functions call UserService.create_user?' or "
                "'Show me all classes that implement the Repository interface'.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "natural_language_query": {
                            "type": "string",
                            "description": "Your question in plain English about the codebase",
                        }
                    },
                    "required": ["natural_language_query"],
                },
                handler=self.query_code_graph,
                returns_json=True,
            ),
            "get_code_snippet": ToolMetadata(
                name="get_code_snippet",
                description="Retrieve source code for a function, class, or method by its qualified name. "
                "Returns the source code, file path, line numbers, and docstring.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "qualified_name": {
                            "type": "string",
                            "description": "Fully qualified name (e.g., 'app.services.UserService.create_user')",
                        }
                    },
                    "required": ["qualified_name"],
                },
                handler=self.get_code_snippet,
                returns_json=True,
            ),
            "surgical_replace_code": ToolMetadata(
                name="surgical_replace_code",
                description="Surgically replace an exact code block in a file using diff-match-patch. "
                "Only modifies the exact target block, leaving the rest unchanged.",
                input_schema={
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
                handler=self.surgical_replace_code,
                returns_json=False,
            ),
            "read_file": ToolMetadata(
                name="read_file",
                description="Read the contents of a file from the project. Supports pagination for large files.",
                input_schema={
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
                handler=self.read_file,
                returns_json=False,
            ),
            "write_file": ToolMetadata(
                name="write_file",
                description="Write content to a file, creating it if it doesn't exist.",
                input_schema={
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
                handler=self.write_file,
                returns_json=False,
            ),
            "list_directory": ToolMetadata(
                name="list_directory",
                description="List contents of a directory in the project.",
                input_schema={
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
                handler=self.list_directory,
                returns_json=False,
            ),
        }

    async def index_repository(self) -> str:
        """Parse and ingest the repository into the Memgraph knowledge graph.

        This tool analyzes the codebase using Tree-sitter parsers and builds
        a comprehensive knowledge graph with functions, classes, dependencies,
        and relationships.

        Note: This clears all existing data in the database before indexing.
        Only one repository can be indexed at a time.

        Returns:
            Success message with indexing statistics
        """
        logger.info(f"[MCP] Indexing repository at: {self.project_root}")

        try:
            # Clear existing data to ensure clean state for the new repository
            logger.info("[MCP] Clearing existing database to avoid conflicts...")
            self.ingestor.clean_database()
            logger.info("[MCP] Database cleared. Starting fresh indexing...")

            updater = GraphUpdater(
                ingestor=self.ingestor,
                repo_path=Path(self.project_root),
                parsers=self.parsers,
                queries=self.queries,
            )
            updater.run()

            return f"Successfully indexed repository at {self.project_root}. Knowledge graph has been updated (previous data cleared)."
        except Exception as e:
            logger.error(f"[MCP] Error indexing repository: {e}")
            return f"Error indexing repository: {str(e)}"

    async def query_code_graph(self, natural_language_query: str) -> dict[str, Any]:
        """Query the codebase knowledge graph using natural language.

        This tool converts your natural language question into a Cypher query,
        executes it against the knowledge graph, and returns structured results
        with summaries.

        Args:
            natural_language_query: Your question in plain English (e.g.,
                "What functions call UserService.create_user?")

        Returns:
            Dictionary containing:
                - cypher_query: The generated Cypher query
                - results: List of result rows from the graph
                - summary: Natural language summary of findings
        """
        logger.info(f"[MCP] query_code_graph: {natural_language_query}")
        try:
            graph_data = await self._query_tool.function(natural_language_query)  # type: ignore[arg-type]
            result_dict = cast(dict[str, Any], graph_data.model_dump())
            logger.info(
                f"[MCP] Query returned {len(result_dict.get('results', []))} results"
            )
            return result_dict
        except Exception as e:
            logger.error(f"[MCP] Error querying code graph: {e}", exc_info=True)
            return {
                "error": str(e),
                "query_used": "N/A",
                "results": [],
                "summary": f"Error executing query: {str(e)}",
            }

    async def get_code_snippet(self, qualified_name: str) -> dict[str, Any]:
        """Retrieve source code for a function, class, or method by qualified name.

        Args:
            qualified_name: Fully qualified name (e.g., "app.services.UserService.create_user")

        Returns:
            Dictionary containing:
                - file_path: Path to the source file
                - src: The source code
                - line_start: Starting line number
                - line_end: Ending line number
                - docstring: Docstring if available
                - found: Whether the code was found
        """
        logger.info(f"[MCP] get_code_snippet: {qualified_name}")
        try:
            snippet = await self._code_tool.function(qualified_name=qualified_name)
            result = snippet.model_dump()
            if result is None:
                return {
                    "error": "Tool returned None",
                    "found": False,
                    "error_message": "Code snippet tool returned an invalid response",
                }
            return cast(dict[str, Any], result)
        except Exception as e:
            logger.error(f"[MCP] Error retrieving code snippet: {e}")
            return {
                "error": str(e),
                "found": False,
                "error_message": str(e),
            }

    async def surgical_replace_code(
        self, file_path: str, target_code: str, replacement_code: str
    ) -> str:
        """Surgically replace an exact code block in a file.

        Uses diff-match-patch algorithm to replace only the exact target block,
        leaving the rest of the file unchanged.

        Args:
            file_path: Relative path to the file from project root
            target_code: Exact code block to replace
            replacement_code: New code to insert

        Returns:
            Success message or error description
        """
        logger.info(f"[MCP] surgical_replace_code in {file_path}")
        try:
            result = await self._file_editor_tool.function(  # type: ignore[call-arg]
                file_path=file_path,
                target_code=target_code,
                replacement_code=replacement_code,
            )
            return cast(str, result)
        except Exception as e:
            logger.error(f"[MCP] Error replacing code: {e}")
            return f"Error: {str(e)}"

    async def read_file(
        self, file_path: str, offset: int | None = None, limit: int | None = None
    ) -> str:
        """Read the contents of a file with optional pagination.

        Args:
            file_path: Relative path to the file from project root
            offset: Line number to start reading from (0-based, optional)
            limit: Maximum number of lines to read (optional)

        Returns:
            File contents (paginated if offset/limit provided) or error message
        """
        logger.info(f"[MCP] read_file: {file_path} (offset={offset}, limit={limit})")
        try:
            # If pagination is requested, use memory-efficient line-by-line reading
            if offset is not None or limit is not None:
                full_path = Path(self.project_root) / file_path
                start = offset if offset is not None else 0

                with open(full_path, encoding="utf-8") as f:
                    # Skip lines before the offset and count how many we actually skipped
                    skipped_count = sum(1 for _ in itertools.islice(f, start))

                    # Read the desired slice of lines
                    if limit is not None:
                        sliced_lines = [line for _, line in zip(range(limit), f)]
                    else:
                        sliced_lines = list(f)

                    paginated_content = "".join(sliced_lines)

                    # Count the remaining lines to get the total without a full second pass
                    remaining_lines_count = sum(1 for _ in f)
                    total_lines = (
                        skipped_count + len(sliced_lines) + remaining_lines_count
                    )

                    # Add metadata header
                    header = f"# Lines {start + 1}-{start + len(sliced_lines)} of {total_lines}\n"
                    return header + paginated_content
            else:
                # No pagination - use the existing file reader tool
                result = await self._file_reader_tool.function(file_path=file_path)  # type: ignore[call-arg]
                return cast(str, result)

        except Exception as e:
            logger.error(f"[MCP] Error reading file: {e}")
            return f"Error: {str(e)}"

    async def write_file(self, file_path: str, content: str) -> str:
        """Write content to a file, creating it if it doesn't exist.

        Args:
            file_path: Relative path to the file from project root
            content: Content to write to the file

        Returns:
            Success message or error description
        """
        logger.info(f"[MCP] write_file: {file_path}")
        try:
            result = await self._file_writer_tool.function(  # type: ignore[call-arg]
                file_path=file_path, content=content
            )
            # Handle FileCreationResult object
            if result.success:
                return f"Successfully wrote file: {file_path}"
            else:
                return f"Error: {result.error_message}"
        except Exception as e:
            logger.error(f"[MCP] Error writing file: {e}")
            return f"Error: {str(e)}"

    async def list_directory(self, directory_path: str = ".") -> str:
        """List contents of a directory.

        Args:
            directory_path: Relative path to directory from project root (default: ".")

        Returns:
            Formatted directory listing or error message
        """
        logger.info(f"[MCP] list_directory: {directory_path}")
        try:
            result = self._directory_lister_tool.function(  # type: ignore[call-arg]
                directory_path=directory_path
            )
            return cast(str, result)
        except Exception as e:
            logger.error(f"[MCP] Error listing directory: {e}")
            return f"Error: {str(e)}"

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Get MCP tool schemas for all registered tools.

        Returns:
            List of tool schema dictionaries suitable for MCP's list_tools()
        """
        return [
            {
                "name": metadata.name,
                "description": metadata.description,
                "inputSchema": metadata.input_schema,
            }
            for metadata in self._tools.values()
        ]

    def get_tool_handler(self, name: str) -> tuple[Callable[..., Any], bool] | None:
        """Get the handler function and return type info for a tool.

        Args:
            name: Tool name to look up

        Returns:
            Tuple of (handler_function, returns_json) or None if tool not found
        """
        metadata = self._tools.get(name)
        if metadata is None:
            return None
        return (metadata.handler, metadata.returns_json)

    def list_tool_names(self) -> list[str]:
        """Get a list of all registered tool names.

        Returns:
            List of tool names
        """
        return list(self._tools.keys())


def create_mcp_tools_registry(
    project_root: str,
    ingestor: MemgraphIngestor,
    cypher_gen: CypherGenerator,
) -> MCPToolsRegistry:
    """Factory function to create an MCP tools registry.

    Args:
        project_root: Path to the target repository
        ingestor: Memgraph ingestor instance
        cypher_gen: Cypher query generator instance

    Returns:
        MCPToolsRegistry instance with all tools initialized
    """
    return MCPToolsRegistry(
        project_root=project_root,
        ingestor=ingestor,
        cypher_gen=cypher_gen,
    )
