"""Lua's import pass: `require` calls, `pcall(require, ...)` and the standard
library's `string.`/`table.` heads into the shared import map, a project file
resolving to its module qn and anything else staying the written path.

`ImportProcessor` owns what every language shares (the import map, the
import sites, the deferred IMPORTS edges and their flush) and hands a Lua
file's imports to the `LuaImporter` it builds."""

from pathlib import Path
from typing import Protocol

from tree_sitter import Node

from ... import constants as cs
from ...node_text import safe_decode_text
from ...types_defs import PropertyDict
from . import utils as lua_utils


def _lua_relative_module(import_path: str, current_module: str) -> str:
    # Resolve `./x` / `../x` against the importing module's package.
    parts = current_module.split(cs.SEPARATOR_DOT)[:-1]
    for p in import_path.replace("\\", cs.SEPARATOR_SLASH).split(cs.SEPARATOR_SLASH):
        if p == cs.PATH_PARENT_DIR:
            if parts:
                parts.pop()
        elif p and p != cs.PATH_CURRENT_DIR:
            parts.append(p)
    return cs.SEPARATOR_DOT.join(parts)


class LuaImportHost(Protocol):
    """What the Lua importer calls on the import processor that builds it:
    the import-site record every language's bindings go through, whose sites
    ride on the IMPORTS edges the processor's flush emits."""

    def _record_import_site(
        self,
        scope_qn: str,
        local_name: str,
        node: Node | None,
        imported_name: str | None = None,
        owner_qn: str | None = None,
    ) -> PropertyDict | None: ...


class LuaImporter:
    __slots__ = ("_host", "repo_path", "project_name", "import_mapping")

    def __init__(
        self,
        host: LuaImportHost,
        repo_path: Path,
        project_name: str,
        import_mapping: dict[str, dict[str, str]],
    ) -> None:
        self._host = host
        self.repo_path = repo_path
        self.project_name = project_name
        # The processor's own map, shared rather than copied: a `require`
        # binds its name in the map every later pass reads.
        self.import_mapping = import_mapping

    def _record_lua_require(
        self,
        module_qn: str,
        call_node: Node,
        module_path: str,
        assigned_name: str | None,
    ) -> None:
        local_name = (
            assigned_name or module_path.rsplit(cs.SEPARATOR_DOT, maxsplit=1)[-1]
        )
        resolved = self._resolve_lua_module_path(module_path, module_qn)
        self.import_mapping[module_qn][local_name] = resolved
        self._host._record_import_site(module_qn, local_name, call_node, module_path)

    def parse_imports(
        self,
        captures: dict[str, list[Node]],
        module_qn: str,
        root_node: Node | None = None,
    ) -> None:
        for call_node in captures.get(cs.CAPTURE_IMPORT, []):
            if self._lua_is_require_call(call_node):
                if module_path := self._lua_extract_require_arg(call_node):
                    self._record_lua_require(
                        module_qn,
                        call_node,
                        module_path,
                        self._lua_extract_assignment_lhs(call_node),
                    )
            elif self._lua_is_pcall_require(call_node):
                if module_path := self._lua_extract_pcall_require_arg(call_node):
                    self._record_lua_require(
                        module_qn,
                        call_node,
                        module_path,
                        lua_utils.extract_pcall_second_identifier(call_node),
                    )

            elif self._lua_is_stdlib_call(call_node):
                if stdlib_module := self._lua_extract_stdlib_module(call_node):
                    self.import_mapping[module_qn][stdlib_module] = stdlib_module
                    self._host._record_import_site(
                        module_qn, stdlib_module, call_node, stdlib_module
                    )

    def _lua_is_require_call(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if first_child and first_child.type == cs.TS_IDENTIFIER:
            return safe_decode_text(first_child) == cs.IMPORT_REQUIRE
        return False

    def _lua_is_pcall_require(self, call_node: Node) -> bool:
        first_child = call_node.children[0] if call_node.children else None
        if not (
            first_child
            and first_child.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_child) == cs.IMPORT_PCALL
        ):
            return False

        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return False

        first_arg_node = next(
            (
                child
                for child in args.children
                if child.type not in cs.PUNCTUATION_TYPES
            ),
            None,
        )

        return (
            first_arg_node is not None
            and first_arg_node.type == cs.TS_IDENTIFIER
            and safe_decode_text(first_arg_node) == cs.IMPORT_REQUIRE
        )

    def _lua_extract_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        candidates = args.children if args else call_node.children
        for node in candidates:
            if node.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(node):
                    return decoded.strip("'\"")
        return None

    def _lua_extract_pcall_require_arg(self, call_node: Node) -> str | None:
        args = call_node.child_by_field_name(cs.FIELD_ARGUMENTS)
        if not args:
            return None
        found_require = False
        for child in args.children:
            if found_require and child.type in cs.LUA_STRING_TYPES:
                if decoded := safe_decode_text(child):
                    return decoded.strip("'\"")
            if (
                child.type == cs.TS_IDENTIFIER
                and safe_decode_text(child) == cs.IMPORT_REQUIRE
            ):
                found_require = True
        return None

    def _lua_extract_assignment_lhs(self, call_node: Node) -> str | None:
        return lua_utils.extract_assigned_name(
            call_node, accepted_var_types=(cs.TS_IDENTIFIER,)
        )

    def _resolve_lua_module_path(self, import_path: str, current_module: str) -> str:
        if import_path.startswith(cs.PATH_RELATIVE_PREFIX) or import_path.startswith(
            cs.PATH_PARENT_PREFIX
        ):
            return _lua_relative_module(import_path, current_module)
        dotted = import_path.replace(cs.SEPARATOR_SLASH, cs.SEPARATOR_DOT)

        try:
            relative_file = (
                dotted.replace(cs.SEPARATOR_DOT, cs.SEPARATOR_SLASH) + cs.EXT_LUA
            )
            if (self.repo_path / relative_file).is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
            if (self.repo_path / f"{dotted}{cs.EXT_LUA}").is_file():
                return f"{self.project_name}{cs.SEPARATOR_DOT}{dotted}"
        except OSError:
            pass

        return dotted

    def _lua_is_stdlib_call(self, call_node: Node) -> bool:
        if not call_node.children:
            return False

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            module_name = safe_decode_text(first_child.children[0])
            return module_name in cs.LUA_STDLIB_MODULES

        return False

    def _lua_extract_stdlib_module(self, call_node: Node) -> str | None:
        if not call_node.children:
            return None

        first_child = call_node.children[0]
        if first_child.type == cs.TS_DOT_INDEX_EXPRESSION and (
            first_child.children and first_child.children[0].type == cs.TS_IDENTIFIER
        ):
            return safe_decode_text(first_child.children[0])

        return None
