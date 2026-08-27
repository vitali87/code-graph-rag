from __future__ import annotations

import asyncio

from loguru import logger
from pydantic_ai import Tool
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import constants as cs
from .. import exceptions as ex
from .. import logs as ls
from ..config import settings
from ..constants import (
    QUERY_NOT_AVAILABLE,
    QUERY_RESULTS_PANEL_TITLE,
    QUERY_SUMMARY_DB_ERROR,
    QUERY_SUMMARY_SUCCESS,
    QUERY_SUMMARY_TIMEOUT,
    QUERY_SUMMARY_TRANSLATION_FAILED,
    QUERY_SUMMARY_TRUNCATED,
)
from ..schemas import QueryGraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator
from ..utils.token_utils import truncate_results_by_tokens
from . import tool_descriptions as td


def scope_rows_to_project(
    rows: list[dict[str, object]], project_name: str | None
) -> list[dict[str, object]]:
    """`rows` restricted to one project, or unchanged when none is given.

    Enforced HERE rather than in the generated Cypher, because the model
    writes that query and may omit the filter -- which is precisely what
    made the reported cross-project bleed intermittent (issue #1494). A
    code-level filter is always-or-never.

    Keyed on `qualified_name`, which every indexed node carries and which
    begins with the project name. A row WITHOUT that key is kept: an
    aggregate (`RETURN count(n)`) has no qualified name, and discarding it
    would turn scoping into silent data loss for every aggregate query.
    """
    if not project_name:
        return rows
    prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    kept: list[dict[str, object]] = []
    for row in rows:
        qualified_name = row.get(cs.IMPORT_QUALIFIED_NAME)
        if not isinstance(qualified_name, str):
            kept.append(row)
        elif qualified_name.startswith(prefix):
            kept.append(row)
    return kept


def create_query_tool(
    ingestor: QueryProtocol,
    cypher_gen: CypherGenerator,
    console: Console | None = None,
    project_name: str | None = None,
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

            results = await asyncio.wait_for(
                asyncio.to_thread(ingestor.fetch_all, cypher_query),
                timeout=settings.QUERY_TIMEOUT_S,
            )

            # Before the row cap and the token truncation, so a scoped query
            # spends its budget on rows the caller can actually use rather
            # than on another project's rows that are about to be dropped.
            results = scope_rows_to_project(results, project_name)

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
        except TimeoutError:
            logger.warning(
                ls.TOOL_QUERY_TIMEOUT.format(
                    timeout=settings.QUERY_TIMEOUT_S, query=cypher_query
                )
            )
            return QueryGraphData(
                query_used=cypher_query,
                results=[],
                summary=QUERY_SUMMARY_TIMEOUT.format(timeout=settings.QUERY_TIMEOUT_S),
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
