from __future__ import annotations

import asyncio
import bisect
import difflib
import re
from pathlib import Path

import diff_match_patch
from loguru import logger
from pydantic_ai import Tool
from tree_sitter import Node, Parser

from .. import constants as cs
from .. import logs as ls
from .. import tool_errors as te
from ..decorators import validate_project_path
from ..language_spec import get_language_for_extension, get_language_spec
from ..models import LanguageSpec
from ..parser_loader import load_parsers
from ..schemas import EditResult
from ..types_defs import AfterWrite, FunctionMatch
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


def _node_source(node: Node) -> str | None:
    return node.text.decode(cs.ENCODING_UTF8) if node.text is not None else None


def _node_name(node: Node) -> str | None:
    name_node = node.child_by_field_name(cs.FIELD_NAME)
    if name_node and name_node.text:
        return name_node.text.decode(cs.ENCODING_UTF8)
    return None


def _collect_function_matches(
    node: Node,
    parent_class: str | None,
    lang_config: LanguageSpec,
    function_name: str,
    out: list[FunctionMatch],
) -> None:
    # Every function whose simple or class-qualified name is `function_name`.
    # A named function is a leaf: nested functions are not searched.
    if node.type in lang_config.function_node_types and (func_name := _node_name(node)):
        qualified_name = f"{parent_class}.{func_name}" if parent_class else func_name
        if function_name in (func_name, qualified_name):
            out.append(
                {
                    "node": node,
                    "simple_name": func_name,
                    "qualified_name": qualified_name,
                    "parent_class": parent_class,
                    "line_number": node.start_point[0] + 1,
                }
            )
        return
    current_class = parent_class
    if node.type in lang_config.class_node_types:
        current_class = _node_name(node) or parent_class
    for child in node.children:
        _collect_function_matches(child, current_class, lang_config, function_name, out)


def _match_at_line(
    matches: list[FunctionMatch], function_name: str, line_number: int
) -> FunctionMatch | None:
    found = next((m for m in matches if m["line_number"] == line_number), None)
    if found is None:
        logger.warning(
            ls.EDITOR_FUNC_NOT_FOUND_AT_LINE.format(
                name=function_name, line=line_number
            )
        )
    return found


def _match_by_qualified_name(
    matches: list[FunctionMatch], function_name: str
) -> FunctionMatch | None:
    found = next((m for m in matches if m["qualified_name"] == function_name), None)
    if found is None:
        logger.warning(ls.EDITOR_FUNC_NOT_FOUND_QN.format(name=function_name))
    return found


def _select_function_match(
    matches: list[FunctionMatch],
    function_name: str,
    line_number: int | None,
    file_path: str,
) -> FunctionMatch | None:
    # One match wins outright; several are disambiguated by line, then by a
    # qualified name, else the first is used with an ambiguity warning.
    if len(matches) <= 1:
        return matches[0] if matches else None
    if line_number is not None:
        return _match_at_line(matches, function_name, line_number)
    if cs.SEPARATOR_DOT in function_name:
        return _match_by_qualified_name(matches, function_name)
    details = [f"'{m['qualified_name']}' at line {m['line_number']}" for m in matches]
    logger.warning(
        ls.EDITOR_AMBIGUOUS.format(
            name=function_name,
            path=file_path,
            count=len(matches),
            details=", ".join(details),
        )
    )
    return matches[0]


class FileEditor:
    __slots__ = ("project_root", "dmp", "parsers", "_write_lock")

    def __init__(self, project_root: str = ".") -> None:
        self.project_root = Path(project_root).resolve()
        self.dmp = diff_match_patch.diff_match_patch()
        self.parsers, _ = load_parsers()
        # ponytail: one lock serialises all async writes; per-path locks if
        # write contention ever matters.
        self._write_lock = asyncio.Lock()
        logger.info(ls.FILE_EDITOR_INIT.format(root=self.project_root))

    def _get_real_extension(self, file_path_obj: Path) -> str:
        extension = file_path_obj.suffix
        if extension == cs.TMP_EXTENSION:
            base_name = file_path_obj.stem
            if cs.SEPARATOR_DOT in base_name:
                return cs.SEPARATOR_DOT + base_name.split(cs.SEPARATOR_DOT)[-1]
        return extension

    def get_parser(self, file_path: str) -> Parser | None:
        file_path_obj = Path(file_path)
        extension = self._get_real_extension(file_path_obj)

        lang_name = get_language_for_extension(extension)
        return self.parsers.get(lang_name) if lang_name else None

    def get_ast(self, file_path: str) -> Node | None:
        parser = self.get_parser(file_path)
        if not parser:
            logger.warning(ls.EDITOR_NO_PARSER.format(path=file_path))
            return None

        with open(file_path, "rb") as f:
            content = f.read()

        tree = parser.parse(content)
        return tree.root_node

    def get_function_source_code(
        self, file_path: str, function_name: str, line_number: int | None = None
    ) -> str | None:
        root_node = self.get_ast(file_path)
        if not root_node:
            return None

        file_path_obj = Path(file_path)
        extension = self._get_real_extension(file_path_obj)

        lang_config = get_language_spec(extension)
        if not lang_config:
            logger.warning(ls.EDITOR_NO_LANG_CONFIG.format(ext=extension))
            return None

        matching_functions: list[FunctionMatch] = []
        _collect_function_matches(
            root_node, None, lang_config, function_name, matching_functions
        )
        match = _select_function_match(
            matching_functions, function_name, line_number, file_path
        )
        return _node_source(match["node"]) if match is not None else None

    def get_diff(
        self,
        file_path: str,
        function_name: str,
        new_code: str,
        line_number: int | None = None,
    ) -> str | None:
        original_code = self.get_function_source_code(
            file_path, function_name, line_number
        )
        if not original_code:
            return None

        diffs = self.dmp.diff_main(original_code, new_code)
        self.dmp.diff_cleanupSemantic(diffs)

        diff = difflib.unified_diff(
            original_code.splitlines(keepends=True),
            new_code.splitlines(keepends=True),
            fromfile=f"original/{function_name}",
            tofile=f"new/{function_name}",
        )
        return "".join(diff)

    def apply_patch_to_file(self, file_path: str, patch_text: str) -> bool:
        try:
            with open(file_path, encoding=cs.ENCODING_UTF8) as f:
                original_content = f.read()

            patches = self.dmp.patch_fromText(patch_text)

            new_content, results = self.dmp.patch_apply(patches, original_content)

            if not all(results):
                logger.warning(ls.EDITOR_PATCH_FAILED.format(path=file_path))
                return False

            with open(file_path, "w", encoding=cs.ENCODING_UTF8) as f:
                f.write(new_content)

            logger.success(ls.EDITOR_PATCH_SUCCESS.format(path=file_path))
            return True

        except Exception as e:
            logger.error(ls.EDITOR_PATCH_ERROR.format(path=file_path, error=e))
            return False

    def replace_code_block(
        self, file_path: str, target_block: str, replacement_block: str
    ) -> bool:
        return self.apply_code_block(file_path, target_block, replacement_block).success

    def apply_code_block(
        self, file_path: str, target_block: str, replacement_block: str
    ) -> EditResult:
        logger.info(ls.TOOL_FILE_EDIT_SURGICAL.format(path=file_path))
        failed = EditResult(
            file_path=file_path,
            error_message=cs.MSG_SURGICAL_FAILED.format(path=file_path),
        )
        try:
            full_path = (self.project_root / file_path).resolve()
            full_path.relative_to(self.project_root)

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

            patches = self.dmp.patch_make(original_content, modified_content)
            patched_content, results = self.dmp.patch_apply(patches, original_content)

            if not all(results):
                logger.error(ls.EDITOR_SURGICAL_FAILED)
                return failed

            with open(
                full_path,
                "w",
                encoding=cs.ENCODING_UTF8,
                newline=cs.NEWLINE_UNTRANSLATED,
            ) as f:
                f.write(patched_content)

            logger.success(ls.TOOL_FILE_EDIT_SURGICAL_SUCCESS.format(path=file_path))
            return EditResult(file_path=file_path)

        except ValueError:
            logger.error(ls.FILE_OUTSIDE_ROOT.format(action=cs.FileAction.EDIT))
            return failed
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

    async def edit_file(self, file_path: str, new_content: str) -> EditResult:
        logger.info(ls.TOOL_FILE_EDIT.format(path=file_path))
        return await self._edit_validated(file_path, new_content)

    @validate_project_path(EditResult, path_arg_name="file_path")
    async def _edit_validated(self, file_path: Path, new_content: str) -> EditResult:
        try:
            if not file_path.is_file():
                error_msg = te.FILE_NOT_FOUND_OR_DIR.format(path=file_path)
                logger.warning(ls.FILE_EDITOR_WARN.format(msg=error_msg))
                return EditResult(file_path=str(file_path), error_message=error_msg)

            async with self._write_lock:
                await asyncio.to_thread(
                    file_path.write_text, new_content, encoding=cs.ENCODING_UTF8
                )

            logger.success(ls.TOOL_FILE_EDIT_SUCCESS.format(path=file_path))
            return EditResult(file_path=str(file_path), success=True)

        except Exception as e:
            error_msg = ls.UNEXPECTED.format(error=e)
            logger.error(ls.FILE_EDITOR_ERR_EDIT.format(path=file_path, error=e))
            return EditResult(file_path=str(file_path), error_message=error_msg)


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
