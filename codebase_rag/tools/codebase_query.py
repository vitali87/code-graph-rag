from __future__ import annotations

from loguru import logger
from pydantic_ai import Tool
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ..constants import (
    LOG_TOOL_QUERY_ERROR,
    LOG_TOOL_QUERY_RECEIVED,
    QUERY_NOT_AVAILABLE,
    QUERY_RESULTS_PANEL_TITLE,
    QUERY_SUMMARY_DB_ERROR,
    QUERY_SUMMARY_SUCCESS,
    QUERY_SUMMARY_TRANSLATION_FAILED,
)
from ..errors import LLMGenerationError
from ..schemas import QueryGraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator


def create_query_tool(
    ingestor: QueryProtocol,
    cypher_gen: CypherGenerator,
    console: Console | None = None,
) -> Tool:
    if console is None:
        console = Console(width=None, force_terminal=True)

    async def query_codebase_knowledge_graph(
        natural_language_query: str,
    ) -> QueryGraphData:
        logger.info(LOG_TOOL_QUERY_RECEIVED.format(query=natural_language_query))
        cypher_query = QUERY_NOT_AVAILABLE
        try:
            cypher_query = await cypher_gen.generate(natural_language_query)

            results = ingestor.fetch_all(cypher_query)

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

            summary = QUERY_SUMMARY_SUCCESS.format(count=len(results))
            return QueryGraphData(
                query_used=cypher_query, results=results, summary=summary
            )
        except LLMGenerationError as e:
            return QueryGraphData(
                query_used=QUERY_NOT_AVAILABLE,
                results=[],
                summary=QUERY_SUMMARY_TRANSLATION_FAILED.format(error=e),
            )
        except Exception as e:
            logger.error(LOG_TOOL_QUERY_ERROR.format(error=e), exc_info=True)
            return QueryGraphData(
                query_used=cypher_query,
                results=[],
                summary=QUERY_SUMMARY_DB_ERROR.format(error=e),
            )

    return Tool(
        function=query_codebase_knowledge_graph,
        description="Query the codebase knowledge graph using natural language questions. Ask in plain English about classes, functions, methods, dependencies, or code structure. Examples: 'Find all functions that call each other', 'What classes are in the user module', 'Show me functions with the longest call chains'.",
    )
