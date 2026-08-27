from __future__ import annotations

import asyncio
import re

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
    QUERY_SUMMARY_UNSCOPEABLE,
)
from ..schemas import QueryGraphData
from ..services import QueryProtocol
from ..services.llm import CypherGenerator
from ..utils.token_utils import truncate_results_by_tokens
from . import tool_descriptions as td

# `derive_project_name` builds "<base>__<8 hex digits>". Requiring the whole
# shape, not just the "__" marker, is what stops `__init__` being read as a
# project-qualified name.
_PROJECT_NAME_RE = re.compile(
    rf".+{cs.PROJECT_NAME_DIGEST_MARKER}[0-9a-f]{{{cs.PROJECT_NAME_DIGEST_LEN}}}"
)


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
    projection = _return_clause(cypher_query)
    if not projection:
        return False
    if cs.CYPHER_QUALIFIED_NAME_TOKEN in projection:
        return True
    # A PURE aggregate names nobody, so there is nothing to attribute.
    # `RETURN n.name, count(n)` is not that: the exemption was meant for
    # `RETURN count(n)` alone, and mixing in a bare field leaks exactly
    # what the exemption assumed could not leak.
    terms = [term.strip() for term in projection.split(cs.CHAR_COMMA)]
    return bool(terms) and all(
        any(agg in term for agg in cs.CYPHER_AGGREGATE_TOKENS) for term in terms
    )


def _return_clause(cypher_query: str) -> str:
    """The uppercased RETURN projection, without trailing ORDER BY / LIMIT.

    Evidence has to be in what the query RETURNS. A substring match over
    the whole query counted `WHERE n.qualified_name STARTS WITH ...` as
    evidence while the projection returned only `n.name` -- precisely the
    unattributable rows this guard exists to refuse.
    """
    upper = cypher_query.upper()
    marker = upper.rfind(cs.CYPHER_RETURN_KEYWORD)
    if marker < 0:
        return ""
    projection = upper[marker + len(cs.CYPHER_RETURN_KEYWORD) :]
    for tail in cs.CYPHER_POST_RETURN_KEYWORDS:
        cut = projection.find(tail)
        if cut >= 0:
            projection = projection[:cut]
    return projection.strip()


def _looks_like_a_qualified_name(value: str) -> bool:
    """Whether `value` is a project-qualified name rather than free text.

    `derive_project_name` produces `<base>__<8 hex digits>`, and the FULL
    shape is required -- not merely the `__` marker. `__init__` contains
    that marker, so a marker-only test discarded every row carrying
    Python's most common method name: the too-aggressive direction, which
    fails as badly as leaking.

    Matching on dots alone would be worse still, since a docstring or a
    path may contain them.
    """
    head = value.split(cs.SEPARATOR_DOT, 1)[0]
    return _PROJECT_NAME_RE.fullmatch(head) is not None


def _row_is_outside(row: dict[str, object], prefix: str) -> bool:
    """Whether `row` names any project other than the one `prefix` selects.

    Every string VALUE is inspected, not a fixed list of key names. The
    repo's own queries return `from_qn`/`to_qn`/`caller_qualified_name`,
    and a generated query may label a column anything at all, so keying on
    known names would fail open for precisely the shapes that leak.

    A row naming no project at all -- `RETURN count(n)` -- is kept, since
    it identifies nothing belonging to anyone else.
    """
    return any(_names_another_project(value, prefix) for value in row.values())


def _names_another_project(value: object, prefix: str) -> bool:
    """Whether `value`, at any depth, carries a foreign qualified name.

    Cypher returns lists and maps -- `collect(m.qualified_name)`, a map
    projection -- so inspecting only top-level strings let a whole list of
    another project's names ride through untouched.
    """
    if isinstance(value, str):
        return _looks_like_a_qualified_name(value) and not value.startswith(prefix)
    if isinstance(value, dict):
        return any(_names_another_project(item, prefix) for item in value.values())
    if isinstance(value, list | tuple | set | frozenset):
        return any(_names_another_project(item, prefix) for item in value)
    return False


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

            # A query returning no qualified name yields rows the filter
            # cannot attribute, so it keeps them. Answering a SCOPED
            # request with those would ignore the scope silently, so the
            # guard belongs here as well as in the MCP handler -- the CLI
            # is scoped too when exactly one project is active.
            if project_name is not None and not requires_project_evidence(cypher_query):
                return QueryGraphData(
                    query_used=cypher_query,
                    results=[],
                    summary=QUERY_SUMMARY_UNSCOPEABLE.format(project=project_name),
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
