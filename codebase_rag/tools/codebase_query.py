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
        if _row_is_outside(row, prefix):
            continue
        kept.append(row)
    return kept


def requires_project_evidence(cypher_query: str) -> bool:
    """Whether `cypher_query` returns something a project filter can judge.

    `scope_rows_to_project` decides per row, from the values it is given. A
    query like `RETURN n.name, n.path` hands it rows with no project
    evidence at all, so it cannot tell one project's rows from another's --
    and it keeps them, since it cannot prove them foreign either.

    That gap is closed here rather than there: a SCOPED request whose query
    projects no qualified name is refused, so the caller learns the scope
    could not be honoured instead of silently receiving every project.

    An aggregate is accepted: `RETURN count(n)` exposes no names, so there
    is nothing to leak, and refusing it would make scoping useless for the
    counting queries a caller most often wants.
    """
    upper = cypher_query.upper()
    if cs.CYPHER_QUALIFIED_NAME_TOKEN in upper:
        return True
    return any(agg in upper for agg in cs.CYPHER_AGGREGATE_TOKENS)


def _looks_like_a_qualified_name(value: str) -> bool:
    """Whether `value` is a project-qualified name rather than free text.

    Every project name `derive_project_name` produces ends in `__<digest>`,
    so that marker is what separates a qualified name from a docstring or a
    path that merely contains dots. Matching on dots alone would discard
    legitimate rows -- the too-aggressive direction, which fails just as
    badly as leaking.
    """
    head = value.split(cs.SEPARATOR_DOT, 1)[0]
    return cs.PROJECT_NAME_DIGEST_MARKER in head


def _row_is_outside(row: dict[str, object], prefix: str) -> bool:
    """Whether `row` names any project other than the one `prefix` selects.

    Every string VALUE is inspected, not a fixed list of key names. The
    repo's own queries return `from_qn`/`to_qn`/`caller_qualified_name`,
    and a generated query may label a column anything at all, so keying on
    known names would fail open for precisely the shapes that leak.

    A row naming no project at all -- `RETURN count(n)` -- is kept, since
    it identifies nothing belonging to anyone else.
    """
    return any(
        isinstance(value, str)
        and _looks_like_a_qualified_name(value)
        and not value.startswith(prefix)
        for value in row.values()
    )


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
