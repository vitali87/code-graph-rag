from __future__ import annotations

import difflib
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
from ..parser_loader import load_parsers
from ..schemas import EditResult
from ..types_defs import FunctionMatch
from ..utils.path_utils import validate_allowed_path
from . import tool_descriptions as td


class FileEditor:
    def __init__(self, project_root: str = ".", mode: str = "edit") -> None:
        self.project_root = Path(project_root).resolve()
        self.mode = mode
        self.allowed_roots: frozenset[Path] | None = None
        self.dmp = diff_match_patch.diff_match_patch()
        self.parsers, _ = load_parsers()
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

        def find_function_nodes(node: Node, parent_class: str | None = None) -> None:
            if node.type in lang_config.function_node_types:
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text:
                    func_name = name_node.text.decode(cs.ENCODING_UTF8)

                    qualified_name = (
                        f"{parent_class}.{func_name}" if parent_class else func_name
                    )

                    if function_name in (func_name, qualified_name):
                        matching_functions.append(
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
                name_node = node.child_by_field_name("name")
                if name_node and name_node.text:
                    current_class = name_node.text.decode(cs.ENCODING_UTF8)

            for child in node.children:
                find_function_nodes(child, current_class)

        find_function_nodes(root_node)

        if not matching_functions:
            return None
        if len(matching_functions) == 1:
            node_text = matching_functions[0]["node"].text
            if node_text is None:
                return None
            return str(node_text.decode(cs.ENCODING_UTF8))
        if line_number is not None:
            for func in matching_functions:
                if func["line_number"] == line_number:
                    node_text = func["node"].text
                    if node_text is None:
                        return None
                    return str(node_text.decode(cs.ENCODING_UTF8))
            logger.warning(
                ls.EDITOR_FUNC_NOT_FOUND_AT_LINE.format(
                    name=function_name, line=line_number
                )
            )
            return None

        if cs.SEPARATOR_DOT in function_name:
            for func in matching_functions:
                if func["qualified_name"] == function_name:
                    node_text = func["node"].text
                    if node_text is None:
                        return None
                    return str(node_text.decode(cs.ENCODING_UTF8))
            logger.warning(ls.EDITOR_FUNC_NOT_FOUND_QN.format(name=function_name))
            return None

        function_details = []
        for func in matching_functions:
            details = f"'{func['qualified_name']}' at line {func['line_number']}"
            function_details.append(details)

        logger.warning(
            ls.EDITOR_AMBIGUOUS.format(
                name=function_name,
                path=file_path,
                count=len(matching_functions),
                details=", ".join(function_details),
            )
        )

        node_text = matching_functions[0]["node"].text
        if node_text is None:
            return None
        return str(node_text.decode(cs.ENCODING_UTF8))

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
        if self.mode == "query":
            logger.error(ls.QUERY_MODE_WRITE_BLOCKED.format(path=file_path))
            return False

        logger.info(ls.TOOL_FILE_EDIT_SURGICAL.format(path=file_path))
        try:
            full_path = validate_allowed_path(
                file_path, self.project_root, self.allowed_roots
            )

            if not full_path.is_file():
                logger.error(ls.EDITOR_FILE_NOT_FOUND.format(path=file_path))
                return False

            with open(full_path, encoding=cs.ENCODING_UTF8) as f:
                original_content = f.read()

            if target_block not in original_content:
                logger.error(ls.EDITOR_BLOCK_NOT_FOUND.format(path=file_path))
                logger.debug(ls.EDITOR_LOOKING_FOR.format(block=repr(target_block)))
                return False

            modified_content = original_content.replace(
                target_block, replacement_block, 1
            )

            if original_content.count(target_block) > 1:
                logger.warning(ls.EDITOR_MULTIPLE_OCCURRENCES)

            if original_content == modified_content:
                logger.warning(ls.EDITOR_NO_CHANGES_IDENTICAL)
                return False

            patches = self.dmp.patch_make(original_content, modified_content)
            patched_content, results = self.dmp.patch_apply(patches, original_content)

            if not all(results):
                logger.error(ls.EDITOR_SURGICAL_FAILED)
                return False

            with open(full_path, "w", encoding=cs.ENCODING_UTF8) as f:
                f.write(patched_content)

            logger.success(ls.TOOL_FILE_EDIT_SURGICAL_SUCCESS.format(path=file_path))
            return True

        except PermissionError:
            logger.error(ls.FILE_OUTSIDE_ROOT.format(action=cs.FileAction.EDIT))
            return False
        except Exception as e:
            logger.error(ls.EDITOR_SURGICAL_ERROR.format(error=e))
            return False

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

            with open(file_path, "w", encoding=cs.ENCODING_UTF8) as f:
                f.write(new_content)

            logger.success(ls.TOOL_FILE_EDIT_SUCCESS.format(path=file_path))
            return EditResult(file_path=str(file_path), success=True)

        except Exception as e:
            error_msg = ls.UNEXPECTED.format(error=e)
            logger.error(ls.FILE_EDITOR_ERR_EDIT.format(path=file_path, error=e))
            return EditResult(file_path=str(file_path), error_message=error_msg)


def create_file_editor_tool(file_editor: FileEditor) -> Tool:
    async def replace_code_surgically(
        file_path: str, target_code: str, replacement_code: str
    ) -> str:
        success = file_editor.replace_code_block(
            file_path, target_code, replacement_code
        )
        if success:
            return cs.MSG_SURGICAL_SUCCESS.format(path=file_path)
        return cs.MSG_SURGICAL_FAILED.format(path=file_path)

    return Tool(
        function=replace_code_surgically,
        name=td.AgenticToolName.REPLACE_CODE,
        description=td.FILE_EDITOR,
        requires_approval=True,
    )
