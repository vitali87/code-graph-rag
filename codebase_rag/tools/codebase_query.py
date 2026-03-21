from __future__ import annotations

import asyncio

from loguru import logger
from pydantic_ai import Tool
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import exceptions as ex
from .. import logs as ls
from ..config import settings
from ..constants import (
    QUERY_NOT_AVAILABLE,
    QUERY_RESULTS_PANEL_TITLE,
    QUERY_SUMMARY_DB_ERROR,
    QUERY_SUMMARY_SUCCESS,
    QUERY_SUMMARY_TRANSLATION_FAILED,
    QUERY_SUMMARY_TRUNCATED,
)
from ..schemas import QueryGraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator
from ..utils.token_utils import truncate_results_by_tokens
from . import tool_descriptions as td


def create_query_tool(
    ingestor: QueryProtocol,
    cypher_gen: CypherGenerator,
    console: Console | None = None,
) -> Tool:
    if console is None:
        console = Console(width=None, stderr=True, force_terminal=True)

    async def query_codebase_knowledge_graph(
        natural_language_query: str,
    ) -> QueryGraphData:
        logger.info(ls.TOOL_QUERY_RECEIVED.format(query=natural_language_query))
        cypher_query = QUERY_NOT_AVAILABLE
        try:
            cypher_query = await cypher_gen.generate(natural_language_query)

            results = await asyncio.to_thread(ingestor.fetch_all, cypher_query)

            total_count = len(results)
            if total_count > settings.QUERY_RESULT_ROW_CAP:
                results = results[: settings.QUERY_RESULT_ROW_CAP]

            results, tokens_used, was_truncated = truncate_results_by_tokens(
                results,
                max_tokens=settings.QUERY_RESULT_MAX_TOKENS,
                original_total=total_count,
            )

            if results:
                table = Table(
                    show_header=True,
                    header_style="bold magenta",
                )
                headers = results[0].keys()
                for header in headers:
                    table.add_column(header)

                for row in results:
                    renderable_values = []
                    for value in row.values():
                        if value is None:
                            renderable_values.append("")
                        elif isinstance(value, bool):
                            renderable_values.append("✓" if value else "✗")
                        elif isinstance(value, int | float):
                            renderable_values.append(str(value))
                        else:
                            renderable_values.append(str(value))
                    table.add_row(*renderable_values)

                console.print(
                    Panel(
                        table,
                        title=QUERY_RESULTS_PANEL_TITLE,
                        expand=False,
                    )
                )

            if was_truncated or total_count > len(results):
                summary = QUERY_SUMMARY_TRUNCATED.format(
                    kept=len(results),
                    total=total_count,
                    tokens=tokens_used,
                    max_tokens=settings.QUERY_RESULT_MAX_TOKENS,
                )
            else:
                summary = QUERY_SUMMARY_SUCCESS.format(count=len(results))
            return QueryGraphData(
                query_used=cypher_query, results=results, summary=summary
            )
        except ex.LLMGenerationError as e:
            return QueryGraphData(
                query_used=QUERY_NOT_AVAILABLE,
                results=[],
                summary=QUERY_SUMMARY_TRANSLATION_FAILED.format(error=e),
            )
        except Exception as e:
            logger.exception(ls.TOOL_QUERY_ERROR.format(error=e))
            return QueryGraphData(
                query_used=cypher_query,
                results=[],
                summary=QUERY_SUMMARY_DB_ERROR.format(error=e),
            )

    return Tool(
        function=query_codebase_knowledge_graph,
        name=td.AgenticToolName.QUERY_GRAPH,
        description=td.CODEBASE_QUERY,
    )
