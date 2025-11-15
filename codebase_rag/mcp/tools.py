"""MCP tool wrappers for code-graph-rag.

This module adapts pydantic-ai Tool instances to MCP-compatible functions.
"""

import itertools
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

    async def index_repository(self) -> str:
        """Parse and ingest the repository into the Memgraph knowledge graph.

        This tool analyzes the codebase using Tree-sitter parsers and builds
        a comprehensive knowledge graph with functions, classes, dependencies,
        and relationships.

        Returns:
            Success message with indexing statistics
        """
        logger.info(f"[MCP] Indexing repository at: {self.project_root}")

        try:
            updater = GraphUpdater(
                ingestor=self.ingestor,
                repo_path=Path(self.project_root),
                parsers=self.parsers,
                queries=self.queries,
            )
            updater.run()

            return f"Successfully indexed repository at {self.project_root}. Knowledge graph has been updated."
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
            snippet = await self._code_tool.function(qualified_name=qualified_name)  # type: ignore[call-arg]
            result = snippet.model_dump() if snippet else None
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
                    # Count total lines for metadata (efficient single pass)
                    total_lines = sum(1 for _ in f)
                    f.seek(0)  # Reset to beginning

                    # Skip lines before offset and read limited lines
                    if limit is not None:
                        lines_iter = itertools.islice(f, start, start + limit)
                    else:
                        lines_iter = itertools.islice(f, start, None)

                    sliced_lines = list(lines_iter)
                    paginated_content = "".join(sliced_lines)

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
            if hasattr(result, "success"):
                if result.success:  # type: ignore[union-attr]
                    return f"Successfully wrote file: {file_path}"
                else:
                    return f"Error: {result.error_message}"  # type: ignore[union-attr]
            return cast(str, result)
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
