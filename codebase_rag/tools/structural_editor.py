# Agentic tool wrapper for ast-grep structural replace (#415). Rewrites are
# gated twice: dry_run defaults to a preview, and the tool requires approval
# before any invocation actually touches disk.
from __future__ import annotations

import asyncio
from collections.abc import Callable

from pydantic_ai import Tool

from .. import constants as cs
from ..taint import ReadContentRecord
from ..types_defs import StructuralReplaceChange
from ..utils.dependencies import has_ast_grep
from . import tool_descriptions as td
from .ast_grep_service import AstGrepService


def format_changes(changes: list[StructuralReplaceChange], dry_run: bool) -> str:
    """Render each changed file and its diff under a header stating whether
    the rewrite was a dry-run preview or actually applied."""
    header = (
        cs.AST_GREP_DRY_RUN_HEADER if dry_run else cs.AST_GREP_APPLIED_HEADER
    ).format(count=len(changes))
    bodies = [f"{c['file']} ({c['matches']} match(es))\n{c['diff']}" for c in changes]
    return "\n\n".join([header, *bodies])


def create_structural_editor_tool(
    service: AstGrepService,
    read_record: ReadContentRecord | None = None,
    on_changes: Callable[[list[StructuralReplaceChange]], None] | None = None,
) -> Tool:
    """Build the `structural_replace` tool, recording rewrite diffs in
    `read_record` so they feed the egress taint gate (issue #1128).

    `on_changes` receives the change records (with their `applied` flag)
    before they are formatted, so a caller can learn which files were
    written (the structural delta, issue #1525)."""

    async def structural_replace(
        pattern: str,
        rewrite: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> str:
        """Rewrite by AST pattern, recording the diff it returns."""
        if not has_ast_grep():
            return cs.AST_GREP_NOT_AVAILABLE
        try:
            # offload to a thread: replace does blocking os.walk, file I/O, and
            # CPU-bound AST parsing, which would stall the event loop.
            changes = await asyncio.to_thread(
                service.replace,
                pattern,
                rewrite,
                language=language,
                dry_run=dry_run,
            )
        # catch broadly: ast-grep-py's Rust bindings raise beyond ValueError
        # (RuntimeError and others); report it rather than crash the turn.
        except Exception as e:
            return str(e)
        if not changes:
            return cs.AST_GREP_NO_MATCHES.format(pattern=pattern)
        if on_changes is not None:
            on_changes(changes)
        formatted = format_changes(changes, dry_run)
        if read_record is not None:
            # Diffs carry repository source on both sides; feed the egress
            # taint gate (issue #1128).
            read_record.record(formatted)
        return formatted

    return Tool(
        function=structural_replace,
        name=td.AgenticToolName.STRUCTURAL_REPLACE,
        description=td.STRUCTURAL_EDITOR,
        requires_approval=True,
    )
