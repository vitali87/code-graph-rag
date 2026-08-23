"""Agentic tool exposing structural duplicate detection.

Runs the same engine as `cgr duplicates` and returns a text report the agent
can act on (read the members, propose the shared implementation).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import logs as ls
from ..cypher_queries import CYPHER_LIST_PROJECTS
from ..duplicates import collect_duplicates_with_coverage, default_duplicates_config
from ..types_defs import DuplicateGroup
from . import tool_descriptions as td

if TYPE_CHECKING:
    from ..services import QueryProtocol


def _project_names(ingestor: QueryProtocol) -> list[str]:
    return [
        str(row.get(cs.KEY_NAME) or "")
        for row in ingestor.fetch_all(CYPHER_LIST_PROJECTS)
    ]


def _format_groups(groups: list[DuplicateGroup], limit: int) -> list[str]:
    lines: list[str] = []
    for number, group in enumerate(groups[:limit], start=1):
        lines.append(
            cs.MSG_DUPLICATES_GROUP.format(
                number=number, kind=group["kind"], similarity=group["similarity"]
            )
        )
        lines.extend(
            cs.MSG_DUPLICATES_MEMBER.format(
                qualified_name=member["qualified_name"],
                path=member["path"],
                start=member["start_line"],
                end=member["end_line"],
            )
            for member in group["members"]
        )
    if len(groups) > limit:
        lines.append(cs.MSG_DUPLICATES_TRUNCATED.format(count=len(groups) - limit))
    return lines


def create_find_duplicates_tool(ingestor: QueryProtocol) -> Tool:
    async def find_duplicate_code(
        project: str | None = None,
        threshold: float = cs.DUPLICATES_DEFAULT_THRESHOLD,
        min_size: int = cs.DUPLICATES_DEFAULT_MIN_NODES,
        limit: int = 20,
    ) -> str:
        projects = await asyncio.to_thread(_project_names, ingestor)
        if not projects:
            return cs.MSG_DUPLICATES_NO_PROJECTS
        resolved = project or (projects[0] if len(projects) == 1 else None)
        if resolved is None:
            return cs.MSG_DUPLICATES_AMBIGUOUS.format(projects=projects)

        logger.info(ls.DUPLICATES_SCANNING.format(project_name=resolved))
        config = default_duplicates_config(threshold=threshold, min_nodes=min_size)
        groups, skipped = await asyncio.to_thread(
            collect_duplicates_with_coverage, ingestor, resolved, config
        )

        if not groups:
            response = cs.MSG_DUPLICATES_NONE.format(
                project=resolved, threshold=threshold, min_size=min_size
            )
        else:
            lines = [
                cs.MSG_DUPLICATES_HEADER.format(count=len(groups), project=resolved),
                *_format_groups(groups, limit),
            ]
            response = "\n".join(lines)
        if skipped:
            response = f"{response}\n{cs.MSG_DUPLICATES_SKIPPED.format(count=skipped)}"
        return response

    return Tool(
        find_duplicate_code,
        name=td.AgenticToolName.FIND_DUPLICATE_CODE,
        description=td.FIND_DUPLICATE_CODE,
    )
