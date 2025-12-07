from typing import Any

from loguru import logger
from pydantic_ai import Tool

from ..utils.dependencies import has_semantic_dependencies


def semantic_code_search(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    Search for functions/methods by natural language intent using semantic embeddings.

    Args:
        query: Natural language description of desired functionality
        top_k: Number of results to return

    Returns:
        List of dictionaries with node information:
        [
            {
                "node_id": int,
                "qualified_name": str,
                "type": str,
                "score": float
            }
        ]
    """
    if not has_semantic_dependencies():
        logger.warning(
            "Semantic search requires 'semantic' extra: uv sync --extra semantic"
        )
        return []

    try:
        from ..config import settings
        from ..embedder import embed_code
        from ..services.graph_service import MemgraphIngestor
        from ..vector_store import search_embeddings

        query_embedding = embed_code(query)

        search_results = search_embeddings(query_embedding, top_k=top_k)

        if not search_results:
            logger.info(f"No semantic matches found for query: {query}")
            return []

        node_ids = [node_id for node_id, _ in search_results]

        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT, batch_size=100
        ) as ingestor:
            placeholders = ", ".join(f"${i}" for i in range(len(node_ids)))
            cypher_query = f"""
            MATCH (n)
            WHERE id(n) IN [{placeholders}]
            RETURN id(n) AS node_id, n.qualified_name AS qualified_name,
                   labels(n) AS type, n.name AS name
            ORDER BY n.qualified_name
            """

            params = {str(i): node_id for i, node_id in enumerate(node_ids)}
            results = ingestor._execute_query(cypher_query, params)

            results_map = {res["node_id"]: res for res in results}

            formatted_results = []
            for node_id, score in search_results:  # Preserve order from vector search
                if node_id in results_map:
                    result = results_map[node_id]
                    formatted_results.append(
                        {
                            "node_id": node_id,
                            "qualified_name": result["qualified_name"],
                            "name": result["name"],
                            "type": result["type"][0] if result["type"] else "Unknown",
                            "score": round(
                                score, 3
                            ),  # Use real similarity score from Qdrant
                        }
                    )

            logger.info(f"Found {len(formatted_results)} semantic matches for: {query}")
            return formatted_results

    except Exception as e:
        logger.error(f"Semantic search failed for query '{query}': {e}")
        return []


def get_function_source_code(node_id: int) -> str | None:
    """
    Retrieve source code for a function/method by node ID.

    Args:
        node_id: Memgraph node ID

    Returns:
        Source code string or None if not found
    """
    try:
        from ..config import settings
        from ..services.graph_service import MemgraphIngestor
        from ..utils.source_extraction import (
            extract_source_lines,
            validate_source_location,
        )

        with MemgraphIngestor(
            host=settings.MEMGRAPH_HOST, port=settings.MEMGRAPH_PORT, batch_size=100
        ) as ingestor:
            query = """
            MATCH (m:Module)-[:DEFINES]->(n)
            WHERE id(n) = $node_id
            RETURN n.qualified_name AS qualified_name, n.start_line AS start_line,
                   n.end_line AS end_line, m.path AS path
            """

            results = ingestor._execute_query(query, {"node_id": node_id})

            if not results:
                logger.warning(f"No node found with ID: {node_id}")
                return None

            result = results[0]
            file_path = result.get("path")
            start_line = result.get("start_line")
            end_line = result.get("end_line")

            is_valid, file_path_obj = validate_source_location(
                file_path, start_line, end_line
            )
            if not is_valid or file_path_obj is None:
                logger.warning(
                    f"Missing or invalid source location info for node {node_id}"
                )
                return None

            return extract_source_lines(file_path_obj, start_line, end_line)

    except Exception as e:
        logger.error(f"Failed to get source code for node {node_id}: {e}")
        return None


def create_semantic_search_tool() -> Tool:
    """
    Factory function to create the semantic code search tool.
    """

    async def semantic_search_functions(query: str, top_k: int = 5) -> str:
        """
        Search for functions/methods using natural language descriptions of their purpose.

        Use this tool when you need to find code that performs specific functionality
        based on intent rather than exact names. Perfect for questions like:
        - "Find error handling functions"
        - "Show me authentication-related code"
        - "Where is data validation implemented?"
        - "Find functions that handle file I/O"

        Args:
            query: Natural language description of the desired functionality
            top_k: Maximum number of results to return (default: 5)

        Returns:
            String describing the found functions with their qualified names and similarity scores
        """
        logger.info(f"[Tool:SemanticSearch] Searching for: '{query}'")

        results = semantic_code_search(query, top_k)

        if not results:
            return f"No semantic matches found for query: '{query}'. This could mean:\n1. No functions match this description\n2. Semantic search dependencies are not installed\n3. No embeddings have been generated yet"

        formatted_results = []
        for i, result in enumerate(results, 1):
            formatted_results.append(
                f"{i}. {result['qualified_name']} (type: {result['type']}, score: {result['score']})"
            )

        response = f"Found {len(results)} semantic matches for '{query}':\n\n"
        response += "\n".join(formatted_results)
        response += "\n\nUse the qualified names above with other tools to get more details or source code."

        return response

    return Tool(semantic_search_functions, name="semantic_search_functions")


def create_get_function_source_tool() -> Tool:
    """
    Factory function to create the function source code retrieval tool.
    """

    async def get_function_source_by_id(node_id: int) -> str:
        """
        Retrieve the complete source code for a function or method by its node ID.

        Use this tool after semantic search to get the actual implementation
        of functions you're interested in.

        Args:
            node_id: The Memgraph node ID of the function/method

        Returns:
            The complete source code of the function/method
        """
        logger.info(
            f"[Tool:GetFunctionSource] Retrieving source for node ID: {node_id}"
        )

        source_code = get_function_source_code(node_id)

        if source_code is None:
            return f"Could not retrieve source code for node ID {node_id}. The node may not exist or source file may be unavailable."

        return f"Source code for node ID {node_id}:\n\n```\n{source_code}\n```"

    return Tool(get_function_source_by_id, name="get_function_source_by_id")
