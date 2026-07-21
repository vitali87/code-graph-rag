from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..cypher_queries import (
    CYPHER_GET_FUNCTION_SOURCE_LOCATION,
    CYPHER_LIST_PROJECTS,
    build_nodes_by_ids_query,
)
from ..types_defs import SemanticSearchResult
from ..utils.dependencies import has_semantic_dependencies
from ..utils.path_utils import (
    absolute_path_within_project_root,
    project_roots_from_rows,
)
from . import tool_descriptions as td

if TYPE_CHECKING:
    from ..services import QueryProtocol


def semantic_code_search(
    ingestor: QueryProtocol,
    query: str,
    top_k: int = 5,
    project: str | None = None,
) -> list[SemanticSearchResult]:
    if not has_semantic_dependencies():
        logger.warning(ex.SEMANTIC_EXTRA)
        return []

    try:
        from ..embedder import embed_code
        from ..vector_store import search_embeddings

        query_embedding = embed_code(query)

        search_results = search_embeddings(
            query_embedding, top_k=top_k, project=project
        )

        if not search_results:
            logger.info(ls.SEMANTIC_NO_MATCH.format(query=query))
            return []

        node_ids = [node_id for node_id, _ in search_results]

        cypher_query = build_nodes_by_ids_query(node_ids)
        params = {str(i): node_id for i, node_id in enumerate(node_ids)}
        results = ingestor.fetch_all(cypher_query, params)

        results_map = {
            node_id: res
            for res in results
            if isinstance((node_id := res.get("node_id")), int)
        }

        formatted_results: list[SemanticSearchResult] = []
        for node_id, score in search_results:
            if node_id in results_map:
                result = results_map[node_id]
                result_type = result.get("type")
                type_str = (
                    result_type[0]
                    if isinstance(result_type, list) and result_type
                    else cs.SEMANTIC_TYPE_UNKNOWN
                )
                formatted_results.append(
                    SemanticSearchResult(
                        node_id=node_id,
                        qualified_name=str(result.get("qualified_name", "")),
                        name=str(result.get("name", "")),
                        type=type_str,
                        score=round(score, 3),
                    )
                )

        logger.info(ls.SEMANTIC_FOUND.format(count=len(formatted_results), query=query))
        return formatted_results

    except Exception as e:
        logger.error(ls.SEMANTIC_FAILED.format(query=query, error=e))
        return []


def get_function_source_code(ingestor: QueryProtocol, node_id: int) -> str | None:
    try:
        from ..utils.source_extraction import (
            extract_source_lines,
            validate_source_location,
        )

        results = ingestor.fetch_all(
            CYPHER_GET_FUNCTION_SOURCE_LOCATION, {"node_id": node_id}
        )

        if not results:
            logger.warning(ls.SEMANTIC_NODE_NOT_FOUND.format(id=node_id))
            return None

        result = results[0]
        file_path = result.get("path")
        start_line = result.get("start_line")
        end_line = result.get("end_line")

        is_valid, file_path_obj = validate_source_location(
            file_path, start_line, end_line
        )
        if not is_valid or file_path_obj is None:
            logger.warning(ls.SEMANTIC_INVALID_LOCATION.format(id=node_id))
            return None

        # The recorded absolute_path is authoritative: a same-named file in
        # the process CWD must not shadow the indexed node. The relative
        # path covers repos moved since indexing and old graphs without the
        # property (issue #425).
        absolute_path = result.get("absolute_path")
        if absolute_path and not absolute_path_within_project_root(
            str(result.get("qualified_name", "")),
            absolute_path,
            project_roots_from_rows(ingestor.fetch_all(CYPHER_LIST_PROJECTS)),
        ):
            absolute_path = None
        if absolute_path and Path(absolute_path).is_file():
            file_path_obj = Path(absolute_path)

        return extract_source_lines(file_path_obj, start_line, end_line)

    except Exception as e:
        logger.error(ls.SEMANTIC_SOURCE_FAILED.format(id=node_id, error=e))
        return None


def create_semantic_search_tool(ingestor: QueryProtocol) -> Tool:
    async def semantic_search_functions(
        query: str, top_k: int = 5, project: str | None = None
    ) -> str:
        logger.info(ls.SEMANTIC_TOOL_SEARCH.format(query=query))

        results = await asyncio.to_thread(
            semantic_code_search, ingestor, query, top_k, project
        )

        if not results:
            return cs.MSG_SEMANTIC_NO_RESULTS.format(query=query)

        formatted_results = []
        for i, result in enumerate(results, 1):
            formatted_results.append(
                f"{i}. {result['qualified_name']} (type: {result['type']}, score: {result['score']})"
            )

        response = cs.MSG_SEMANTIC_RESULT_HEADER.format(count=len(results), query=query)
        response += "\n".join(formatted_results)
        response += cs.MSG_SEMANTIC_RESULT_FOOTER

        return response

    return Tool(
        semantic_search_functions,
        name=td.AgenticToolName.SEMANTIC_SEARCH,
        description=td.SEMANTIC_SEARCH,
    )


def create_get_function_source_tool(ingestor: QueryProtocol) -> Tool:
    async def get_function_source_by_id(node_id: int) -> str:
        logger.info(ls.SEMANTIC_TOOL_SOURCE.format(id=node_id))

        source_code = await asyncio.to_thread(
            get_function_source_code, ingestor, node_id
        )

        if source_code is None:
            return cs.MSG_SEMANTIC_SOURCE_UNAVAILABLE.format(id=node_id)

        return cs.MSG_SEMANTIC_SOURCE_FORMAT.format(id=node_id, code=source_code)

    return Tool(
        get_function_source_by_id,
        name=td.AgenticToolName.GET_FUNCTION_SOURCE,
        description=td.GET_FUNCTION_SOURCE,
    )
