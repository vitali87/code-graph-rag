from __future__ import annotations

import asyncio
import bisect
import re
from pathlib import Path

from loguru import logger
from pydantic_ai import Tool

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..schemas import EditResult
from ..types_defs import AfterWrite
from ..utils.path_utils import resolve_in_root
from . import tool_descriptions as td

_LINE_BREAK = re.compile(r"\r\n?")


class _StoredText:
    """A file's text exactly as stored, searched the way a client sees it.

    Clients send targets with "\\n" endings (text-mode reads translate every
    ending), so matching runs on the translated view; the edit is spliced
    into the stored text so every line outside the target keeps its ending.
    """

    __slots__ = ("stored", "view", "_collapsed")

    def __init__(self, stored: str) -> None:
        self.stored = stored
        self.view = _LINE_BREAK.sub(cs.LINE_FEED, stored)
        # Each CRLF is one character in the view: record the view offset of
        # each, so a view offset maps back to the stored one.
        self._collapsed = [
            m.start() - i for i, m in enumerate(re.finditer(cs.CRLF, stored))
        ]

    def stored_offset(self, view_offset: int) -> int:
        return view_offset + bisect.bisect_left(self._collapsed, view_offset)

    def newline(self) -> str:
        # The ending most of the file's lines use; ties go to "\n".
        crlf = self.stored.count(cs.CRLF)
        counts = {
            cs.LINE_FEED: self.stored.count(cs.LINE_FEED) - crlf,
            cs.CRLF: crlf,
            cs.CARRIAGE_RETURN: self.stored.count(cs.CARRIAGE_RETURN) - crlf,
        }
        return max(counts, key=counts.__getitem__)

    def match_starts(self, target: str) -> list[int]:
        # Overlapping matches count: "x\nx" in "x\nx\nx" is two places.
        starts: list[int] = []
        found = self.view.find(target)
        while found != -1:
            starts.append(found)
            found = self.view.find(target, found + 1)
        return starts

    def line_of(self, view_offset: int) -> int:
        return self.view.count(cs.LINE_FEED, 0, view_offset) + 1


def _in_view(code: str) -> str:
    return _LINE_BREAK.sub(cs.LINE_FEED, code)


def _ambiguous_lines(text: _StoredText, starts: list[int]) -> str:
    lines = sorted({text.line_of(start) for start in starts})
    shown = cs.SEPARATOR_COMMA_SPACE.join(
        str(line) for line in lines[: cs.SURGICAL_MATCH_LINES_SHOWN]
    )
    if len(lines) > cs.SURGICAL_MATCH_LINES_SHOWN:
        shown += cs.SURGICAL_MORE_LINES
    return shown


class FileEditor:
    __slots__ = ("project_root", "_write_lock")

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = Path(project_root).resolve()
        # ponytail: one lock serialises all async writes; per-path locks if
        # write contention ever matters.
        self._write_lock = asyncio.Lock()
        logger.info(ls.FILE_EDITOR_INIT.format(root=self.project_root))

    def apply_code_block(
        self, file_path: str, target_block: str, replacement_block: str
    ) -> EditResult:
        logger.info(ls.TOOL_FILE_EDIT_SURGICAL.format(path=file_path))
        failed = EditResult(
            file_path=file_path,
            error_message=cs.MSG_SURGICAL_FAILED.format(path=file_path),
        )
        full_path = resolve_in_root(self.project_root, file_path)
        if full_path is None:
            logger.error(ls.FILE_OUTSIDE_ROOT.format(action=cs.FileAction.EDIT))
            return EditResult(
                file_path=file_path,
                error_message=te.PATH_OUTSIDE_ROOT.format(
                    path=file_path, root=self.project_root
                ),
            )
        try:
            if not full_path.is_file():
                logger.error(ls.EDITOR_FILE_NOT_FOUND.format(path=file_path))
                return failed

            with open(
                full_path, encoding=cs.ENCODING_UTF8, newline=cs.NEWLINE_UNTRANSLATED
            ) as f:
                text = _StoredText(f.read())

            target = _in_view(target_block)
            starts = text.match_starts(target)
            if not starts:
                logger.error(ls.EDITOR_BLOCK_NOT_FOUND.format(path=file_path))
                logger.debug(ls.EDITOR_LOOKING_FOR, block=repr(target_block))
                return failed

            # Replacing the first of several matches edits a place the caller
            # may not have meant, so an ambiguous target changes nothing.
            if len(starts) > 1:
                logger.warning(
                    ls.EDITOR_MULTIPLE_OCCURRENCES.format(
                        path=file_path, count=len(starts)
                    )
                )
                return EditResult(
                    file_path=file_path,
                    error_message=cs.MSG_SURGICAL_AMBIGUOUS.format(
                        path=file_path,
                        count=len(starts),
                        lines=_ambiguous_lines(text, starts),
                    ),
                )

            begin = text.stored_offset(starts[0])
            end = text.stored_offset(starts[0] + len(target))
            replacement = _in_view(replacement_block).replace(
                cs.LINE_FEED, text.newline()
            )
            original_content = text.stored
            modified_content = (
                original_content[:begin] + replacement + original_content[end:]
            )

            if original_content == modified_content:
                logger.warning(ls.EDITOR_NO_CHANGES_IDENTICAL)
                return failed

            with open(
                full_path,
                "w",
                encoding=cs.ENCODING_UTF8,
                newline=cs.NEWLINE_UNTRANSLATED,
            ) as f:
                f.write(modified_content)

            logger.success(ls.TOOL_FILE_EDIT_SURGICAL_SUCCESS.format(path=file_path))
            return EditResult(file_path=file_path)

        except Exception as e:
            logger.error(ls.EDITOR_SURGICAL_ERROR.format(error=e))
            return failed

    async def replace_code_block_async(
        self, file_path: str, target_block: str, replacement_block: str
    ) -> EditResult:
        async with self._write_lock:
            return await asyncio.to_thread(
                self.apply_code_block, file_path, target_block, replacement_block
            )


def create_file_editor_tool(
    file_editor: FileEditor, after_write: AfterWrite | None = None
) -> Tool:
    async def replace_code_surgically(
        file_path: str, target_code: str, replacement_code: str
    ) -> str:
        result = await file_editor.replace_code_block_async(
            file_path, target_code, replacement_code
        )
        if not result.success:
            return te.ToolFailure(
                result.error_message or cs.MSG_SURGICAL_FAILED.format(path=file_path)
            )
        message = cs.MSG_SURGICAL_SUCCESS.format(path=file_path)
        # The chat session re-ingests what it writes, so its next question
        # reads the edited code (issue #2916).
        if after_write is not None:
            message += await after_write([file_path])
        return message

    return Tool(
        function=replace_code_surgically,
        name=td.AgenticToolName.REPLACE_CODE,
        description=td.FILE_EDITOR,
        requires_approval=True,
    )
